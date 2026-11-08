import os
import re
import asyncio
import secrets
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('dodo_helper')
TOKEN = os.environ['DISCORD_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
GUILD_ID = int(os.getenv('GUILD_ID') or 0)
MENTION_ONLY = re.compile(r'<@!?(\d+)>')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
pool = None

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('giveaway','first')),
 guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, message_id BIGINT,
 host_id BIGINT NOT NULL, title TEXT NOT NULL, capacity INT NOT NULL,
 ends_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 winners BIGINT[] NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS entries (
 event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 user_id BIGINT NOT NULL, joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(event_id,user_id)
);
CREATE INDEX IF NOT EXISTS idx_events_open ON events(status,ends_at);
'''

async def get_event(event_id, conn=None):
    if conn is not None:
        return await conn.fetchrow('SELECT * FROM events WHERE id=$1', event_id)
    async with pool.acquire() as c:
        return await c.fetchrow('SELECT * FROM events WHERE id=$1', event_id)

async def members(event_id):
    async with pool.acquire() as c:
        return [r['user_id'] for r in await c.fetch('SELECT user_id FROM entries WHERE event_id=$1 ORDER BY joined_at,user_id', event_id)]

def event_embed(e, ids):
    kind = e['kind']
    status = e['status']
    title = '🎁 무료 나눔' if kind == 'giveaway' else '🎟️ 선착순 모집'
    embed = discord.Embed(title=title, description=f'**{discord.utils.escape_markdown(e["title"])}**', color=0x438FEA)
    embed.add_field(name='주최자', value=f'<@{e["host_id"]}>')
    embed.add_field(name='당첨 인원' if kind == 'giveaway' else '정원', value=str(e['capacity']))
    embed.add_field(name='참여 인원', value=str(len(ids)))
    embed.add_field(name='마감', value=f'<t:{int(e["ends_at"].timestamp())}:F>')
    embed.add_field(name='상태', value={'open':'🟢 진행 중','closed':'🔴 마감','cancelled':'⚫ 취소'}[status], inline=False)
    if kind == 'first':
        visible = '\n'.join(f'{i}. <@{uid}>' for i,uid in enumerate(ids[:20],1)) or '아직 참여자가 없습니다.'
        if len(ids)>20: visible += f'\n외 {len(ids)-20}명'
        embed.add_field(name='참여 순서',value=visible,inline=False)
    if kind == 'giveaway' and e['winners']:
        embed.add_field(name='당첨자',value='\n'.join(f'<@{u}>' for u in e['winners']),inline=False)
    embed.set_footer(text=f'이벤트 #{e["id"]} · 공식 도도 재화 자동 지급 없음')
    return embed

class EventView(discord.ui.View):
    def __init__(self,event_id,disabled=False):
        super().__init__(timeout=None)
        self.event_id=event_id
        for item in self.children:
            if isinstance(item,discord.ui.Button):
                item.custom_id=f'dodo:{item.custom_id}:{event_id}'
                item.disabled=disabled

    @discord.ui.button(label='참여',style=discord.ButtonStyle.success,custom_id='join',emoji='🙋')
    async def join(self,interaction:discord.Interaction,button:discord.ui.Button):
        await handle_entry(interaction,self.event_id,True)

    @discord.ui.button(label='참여 취소',style=discord.ButtonStyle.secondary,custom_id='leave',emoji='↩️')
    async def leave(self,interaction:discord.Interaction,button:discord.ui.Button):
        await handle_entry(interaction,self.event_id,False)

    @discord.ui.button(label='참여자 명단',style=discord.ButtonStyle.primary,custom_id='list',emoji='📋')
    async def listing(self,interaction:discord.Interaction,button:discord.ui.Button):
        e=await get_event(self.event_id)
        if not e:
            await interaction.response.send_message('이벤트를 찾지 못했습니다.',ephemeral=True);return
        ids=await members(self.event_id)
        lines=[f'{i}. <@{uid}>' for i,uid in enumerate(ids,1)]
        if not lines: lines=['참여자가 없습니다.']
        chunks=[]; current=''
        for line in lines:
            if len(current)+len(line)+1>1700:
                chunks.append(current);current=''
            current+=line+'\n'
        if current: chunks.append(current)
        await interaction.response.send_message(f'**참여자 명단 · 총 {len(ids)}명**\n{chunks[0]}',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

async def refresh(event_id):
    e=await get_event(event_id)
    if not e or not e['message_id']: return
    try:
        channel=bot.get_channel(e['channel_id']) or await bot.fetch_channel(e['channel_id'])
        msg=await channel.fetch_message(e['message_id'])
        ids=await members(event_id)
        await msg.edit(embed=event_embed(e,ids),view=EventView(event_id,disabled=e['status']!='open'),allowed_mentions=discord.AllowedMentions.none())
    except (discord.NotFound,discord.Forbidden,discord.HTTPException) as exc:
        log.warning('Cannot refresh event %s: %s',event_id,exc)

async def handle_entry(interaction,event_id,joining):
    if interaction.guild is None:
        await interaction.response.send_message('서버에서만 이용할 수 있습니다.',ephemeral=True);return
    await interaction.response.defer(ephemeral=True,thinking=True)
    uid=interaction.user.id
    result=''
    async with pool.acquire() as c:
        async with c.transaction():
            e=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',event_id)
            if not e or e['guild_id']!=interaction.guild_id:
                result='이벤트를 찾을 수 없습니다.'
            elif e['status']!='open' or e['ends_at']<=datetime.now(timezone.utc):
                result='이미 마감된 이벤트입니다.'
            elif joining:
                exists=await c.fetchval('SELECT 1 FROM entries WHERE event_id=$1 AND user_id=$2',event_id,uid)
                count=await c.fetchval('SELECT COUNT(*) FROM entries WHERE event_id=$1',event_id)
                if exists: result='이미 참여 중입니다.'
                elif e['kind']=='first' and count>=e['capacity']: result='정원이 마감되었습니다.'
                else:
                    await c.execute('INSERT INTO entries(event_id,user_id) VALUES($1,$2)',event_id,uid)
                    result='참여 완료!'
                    if e['kind']=='first' and count+1>=e['capacity']:
                        await c.execute("UPDATE events SET status='closed' WHERE id=$1",event_id)
                        result+=' 정원이 차서 모집이 마감되었습니다.'
            else:
                deleted=await c.execute('DELETE FROM entries WHERE event_id=$1 AND user_id=$2',event_id,uid)
                result='참여를 취소했습니다.' if deleted.endswith('1') else '참여 기록이 없습니다.'
    await interaction.followup.send(result,ephemeral=True)
    await refresh(event_id)

async def finish_event(event_id):
    announcement=None
    async with pool.acquire() as c:
        async with c.transaction():
            e=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',event_id)
            if not e or e['status']!='open' or e['ends_at']>datetime.now(timezone.utc):return
            ids=[r['user_id'] for r in await c.fetch('SELECT user_id FROM entries WHERE event_id=$1 ORDER BY joined_at,user_id',event_id)]
            winners=secrets.SystemRandom().sample(ids,min(e['capacity'],len(ids))) if e['kind']=='giveaway' else []
            await c.execute("UPDATE events SET status='closed',winners=$2 WHERE id=$1",event_id,winners)
            if e['kind']=='giveaway':
                if winners: announcement='🎉 **나눔 당첨자 발표**\n'+ '\n'.join(f'<@{u}>' for u in winners)
                else: announcement='🎁 나눔이 종료되었습니다. 참여자가 없어 당첨자가 없습니다.'
    await refresh(event_id)
    if announcement:
        try:
            ch=bot.get_channel(e['channel_id']) or await bot.fetch_channel(e['channel_id'])
            await ch.send(announcement,allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False))
        except (discord.Forbidden,discord.HTTPException) as exc:
            log.warning('Cannot announce %s: %s',event_id,exc)

@tasks.loop(seconds=15)
async def expiry_loop():
    async with pool.acquire() as c:
        ids=await c.fetch("SELECT id FROM events WHERE status='open' AND ends_at<=NOW() ORDER BY ends_at LIMIT 100")
    for row in ids:
        try: await finish_event(row['id'])
        except Exception: log.exception('Error finishing event %s',row['id'])

@expiry_loop.before_loop
async def before_expiry():
    await bot.wait_until_ready()

async def create_event(interaction,kind,title,capacity,minutes):
    if not interaction.guild or not isinstance(interaction.channel,discord.abc.Messageable):
        await interaction.response.send_message('서버의 텍스트 채널에서 실행해 주세요.',ephemeral=True);return
    if not 1<=capacity<=1000 or not 1<=minutes<=43200 or not title.strip():
        await interaction.response.send_message('인원 1~1000명, 마감 1~43200분, 제목은 필수입니다.',ephemeral=True);return
    if len(title)>150:
        await interaction.response.send_message('제목은 150자 이내로 입력해 주세요.',ephemeral=True);return
    await interaction.response.defer(ephemeral=True)
    end=datetime.now(timezone.utc)+timedelta(minutes=minutes)
    async with pool.acquire() as c:
        event_id=await c.fetchval('INSERT INTO events(kind,guild_id,channel_id,host_id,title,capacity,ends_at) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id',kind,interaction.guild_id,interaction.channel_id,interaction.user.id,title.strip(),capacity,end)
    try:
        e=await get_event(event_id)
        msg=await interaction.channel.send(embed=event_embed(e,[]),view=EventView(event_id),allowed_mentions=discord.AllowedMentions.none())
        async with pool.acquire() as c:
            await c.execute('UPDATE events SET message_id=$2 WHERE id=$1',event_id,msg.id)
    except (discord.Forbidden,discord.HTTPException):
        async with pool.acquire() as c: await c.execute("UPDATE events SET status='cancelled' WHERE id=$1",event_id)
        await interaction.followup.send('게시 권한이 없어 이벤트를 생성하지 못했습니다.',ephemeral=True);return
    bot.add_view(EventView(event_id),message_id=msg.id)
    await interaction.followup.send(f'생성 완료: {msg.jump_url}',ephemeral=True)

@bot.tree.command(name='나눔생성',description='무료 나눔을 개최합니다.')
@app_commands.describe(상품='나눔할 상품이나 내용',당첨인원='뽑을 당첨자 수',마감분='몇 분 후 종료할지')
async def giveaway(interaction:discord.Interaction,상품:str,당첨인원:app_commands.Range[int,1,1000],마감분:app_commands.Range[int,1,43200]):
    await create_event(interaction,'giveaway',상품,당첨인원,마감분)

@bot.tree.command(name='선착순생성',description='선착순 참여 모집을 엽니다.')
@app_commands.describe(내용='모집할 내용',모집인원='선착순 정원',마감분='정원 미달 시 자동 종료까지의 시간')
async def first(interaction:discord.Interaction,내용:str,모집인원:app_commands.Range[int,1,1000],마감분:app_commands.Range[int,1,43200]):
    await create_event(interaction,'first',내용,모집인원,마감분)

@bot.tree.command(name='이벤트취소',description='본인이 개최한 이벤트를 취소합니다.')
async def cancel(interaction:discord.Interaction,이벤트번호:int):
    await interaction.response.defer(ephemeral=True)
    async with pool.acquire() as c:
        async with c.transaction():
            e=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',이벤트번호)
            if not e or e['guild_id']!=interaction.guild_id or e['host_id']!=interaction.user.id:
                result='본인이 개최한 이벤트만 취소할 수 있습니다.'
            elif e['status']!='open': result='이미 종료된 이벤트입니다.'
            else:
                await c.execute("UPDATE events SET status='cancelled' WHERE id=$1",이벤트번호)
                result='이벤트를 취소했습니다.'
    await interaction.followup.send(result,ephemeral=True)
    if result=='이벤트를 취소했습니다.': await refresh(이벤트번호)

@bot.tree.command(name='나눔재추첨',description='종료된 본인 나눔에서 당첨자를 다시 추첨합니다.')
async def redraw(interaction:discord.Interaction,이벤트번호:int):
    await interaction.response.defer(ephemeral=True)
    async with pool.acquire() as c:
        async with c.transaction():
            e=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',이벤트번호)
            if not e or e['guild_id']!=interaction.guild_id or e['host_id']!=interaction.user.id or e['kind']!='giveaway':
                result='본인의 나눔 이벤트만 재추첨할 수 있습니다.'
            elif e['status']!='closed': result='종료된 나눔만 재추첨할 수 있습니다.'
            else:
                ids=[r['user_id'] for r in await c.fetch('SELECT user_id FROM entries WHERE event_id=$1',이벤트번호)]
                if not ids: result='참여자가 없어 재추첨할 수 없습니다.'
                else:
                    winners=secrets.SystemRandom().sample(ids,min(e['capacity'],len(ids)))
                    await c.execute('UPDATE events SET winners=$2 WHERE id=$1',이벤트번호,winners)
                    result='재추첨 완료: '+', '.join(f'<@{u}>' for u in winners)
    await interaction.followup.send(result,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    if result.startswith('재추첨 완료'):
        await refresh(이벤트번호)
        try:
            ch=bot.get_channel(e['channel_id']) or await bot.fetch_channel(e['channel_id'])
            await ch.send('🔄 **나눔 재추첨 결과**\n'+'\n'.join(f'<@{u}>' for u in winners),allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False))
        except (discord.Forbidden,discord.HTTPException): pass

@bot.event
async def on_message(message:discord.Message):
    if message.author.bot or message.guild is None:return
    match=MENTION_ONLY.fullmatch(message.content.strip())
    if not match or len(message.mentions)!=1 or message.mention_everyone or message.role_mentions:return
    if int(match.group(1))!=message.mentions[0].id:return
    await message.channel.send(match.group(1), allowed_mentions=discord.AllowedMentions.none())

@bot.event
async def on_ready():
    log.info('Ready as %s',bot.user)

async def main():
    global pool
    pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=5,command_timeout=30)
    async with pool.acquire() as c: await c.execute(SCHEMA)
    async with pool.acquire() as c:
        active=await c.fetch("SELECT id,message_id FROM events WHERE status='open' AND message_id IS NOT NULL")
    for e in active:
        bot.add_view(EventView(e['id']),message_id=e['message_id'])
    async with bot:
        await bot.login(TOKEN)
        if GUILD_ID:
            guild=discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        expiry_loop.start()
        await bot.connect()

if __name__=='__main__':
    asyncio.run(main())
