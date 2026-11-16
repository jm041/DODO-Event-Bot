import os
import re
import asyncio
import secrets
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('dodo_helper')
TOKEN = os.environ['DISCORD_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
OWNER_GUILD_ID = 1558462262132351096
OWNER_ID_CHANNEL_IDS = {1566775552076156948, 1566774361736872138}
TRANSFER_LOG_CHANNEL_ID = 1566774361736872138
HOMEWORK_CHANNEL_ID = 1566775227885555742
KST = ZoneInfo("Asia/Seoul")
# Set to the actual Dodo bot user ID in Railway Variables. 0 disables transfer alerts.
DODO_BOT_ID = int(os.getenv("DODO_BOT_ID", "0"))
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
CREATE TABLE IF NOT EXISTS first_announcements (
 event_id BIGINT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS homework_announcements (
 guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, day DATE NOT NULL,
 source_message_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (guild_id,user_id,day), UNIQUE(source_message_id)
);
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

async def announce_first_closed(event_id):
    """Post a final first-come list exactly once after the event closes."""
    async with pool.acquire() as c:
        async with c.transaction():
            e = await c.fetchrow('SELECT * FROM events WHERE id=$1', event_id)
            if not e or e['kind'] != 'first' or e['status'] != 'closed':
                return
            claimed = await c.fetchval(
                'INSERT INTO first_announcements(event_id) VALUES($1) '
                'ON CONFLICT DO NOTHING RETURNING event_id', event_id
            )
            if claimed is None:
                return
    try:
        ids = await members(event_id)
        ch = bot.get_channel(e['channel_id']) or await bot.fetch_channel(e['channel_id'])
        count = len(ids)
        safe_title = discord.utils.escape_markdown(e['title'])
        # Discord embed limits: description 4096, field value 1024, total 6000.
        # Divide large rosters into multiple embeds without changing participant order.
        lines = [f'{i}. <@{uid}>' for i, uid in enumerate(ids, 1)]
        if not lines:
            lines = ['참여자가 없습니다.']
        chunks = []
        current = ''
        for line in lines:
            if len(current) + len(line) + 1 > 900:
                chunks.append(current.rstrip())
                current = ''
            current += line + '\n'
        if current:
            chunks.append(current.rstrip())
        # Limit each embed to 4 roster fields, keeping total embed size safely below 6000.
        pages = [chunks[i:i + 4] for i in range(0, len(chunks), 4)]
        for page_index, page_chunks in enumerate(pages, 1):
            embed = discord.Embed(
                title='🎉 선착순 모집 마감 — 최종 명단',
                description=(f'📌 **모집 내용:** {safe_title[:1000]}\n'
                             f'👥 **최종 참여:** {count}명 / {e["capacity"]}명\n'
                             f'🏁 **마감 사유:** {"정원 마감" if count >= e["capacity"] else "시간 마감"}'),
                color=discord.Color.green(),
            )
            for idx, chunk in enumerate(page_chunks, 1):
                embed.add_field(
                    name='🏆 확정 참여자' if page_index == 1 and idx == 1 else '🏆 확정 참여자 (계속)',
                    value=chunk,
                    inline=False,
                )
            embed.set_footer(text=f'이벤트 #{event_id} · {page_index}/{len(pages)}페이지 · 모집 종료')
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.NotFound:
        # Channel was deleted or is no longer accessible. Keep the claim to avoid
        # retrying the same impossible delivery every 15 seconds.
        log.warning('First-come event %s: original channel %s no longer exists; stopping retries',
                    event_id, e['channel_id'])
    except Exception:
        # Release claim for retry if posting fails transiently.
        async with pool.acquire() as c:
            await c.execute('DELETE FROM first_announcements WHERE event_id=$1', event_id)
        log.exception('Cannot announce first-come event %s', event_id)

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
    await announce_first_closed(event_id)

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
    await announce_first_closed(event_id)
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
    async with pool.acquire() as c:
        pending=await c.fetch("SELECT e.id FROM events e LEFT JOIN first_announcements a ON a.event_id=e.id WHERE e.kind='first' AND e.status='closed' AND a.event_id IS NULL ORDER BY e.id LIMIT 100")
    for row in pending:
        await announce_first_closed(row['id'])

@expiry_loop.before_loop
async def before_expiry():
    await bot.wait_until_ready()

async def create_event(interaction,kind,title,capacity,minutes):
    if interaction.guild_id != OWNER_GUILD_ID:
        await interaction.response.send_message('이 명령어는 운영 서버에서만 사용할 수 있습니다.', ephemeral=True);return
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
    if interaction.guild_id != OWNER_GUILD_ID:
        await interaction.response.send_message('이 명령어는 운영 서버에서만 사용할 수 있습니다.', ephemeral=True);return
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
    if interaction.guild_id != OWNER_GUILD_ID:
        await interaction.response.send_message('이 명령어는 운영 서버에서만 사용할 수 있습니다.', ephemeral=True);return
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

# A transfer can arrive as a new message or an edited interaction response.
# Only trust actual Discord mention IDs, never nicknames or avatar images.
TRANSFER_RECIPIENT = re.compile(r"<@!?(\d{17,20})>")
TRANSFER_AMOUNT = re.compile(r"([\d,]+)\s*원을?\s*송금")
_notified_transfers: set[int] = set()
_transfer_locks: dict[int, asyncio.Lock] = {}

async def notify_transfer_recipient(message: discord.Message):
    if message.guild is None or message.guild.id != OWNER_GUILD_ID:
        return
    if not DODO_BOT_ID or message.author.id != DODO_BOT_ID:
        return
    lock = _transfer_locks.setdefault(message.id, asyncio.Lock())
    async with lock:
        if message.id in _notified_transfers:
            return
        for embed in message.embeds:
            parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
            for field in embed.fields:
                parts.extend((field.name or "", field.value or ""))
            body = "\n".join(parts)
            if "송금 성공" not in body:
                continue
            # Read the success sentence rather than a balance line or fee line.
            lines = body.splitlines()
            recipient_line = next((line for line in lines if "님에게" in line and "송금" in line), "")
            if not recipient_line:
                recipient_line = next((line for line in lines if "님에게" in line), "")
            recipient_match = TRANSFER_RECIPIENT.search(recipient_line)
            # Some bot embeds put the receiver mention in a separate field.
            if not recipient_match:
                recipient_match = TRANSFER_RECIPIENT.search(body)
            if not recipient_match:
                log.warning("Dodo transfer detected, recipient ID not found: msg=%s channel=%s embed=%r mentions=%s",
                            message.id, message.channel.id, body[:900], [u.id for u in message.mentions])
                return
            recipient_id = int(recipient_match.group(1))
            amount_match = TRANSFER_AMOUNT.search(recipient_line)
            if not amount_match:
                amount_match = TRANSFER_AMOUNT.search(body)
            if not amount_match:
                log.warning("Dodo transfer detected, amount not found: msg=%s embed=%r", message.id, body[:900])
                return
            amount = int(amount_match.group(1).replace(",", ""))
            log.info("Dodo transfer detected: msg=%s from_channel=%s recipient=%s amount=%s",
                     message.id, message.channel.id, recipient_id, amount)
            try:
                target = message
                if message.channel.id != TRANSFER_LOG_CHANNEL_ID:
                    log_channel = message.guild.get_channel(TRANSFER_LOG_CHANNEL_ID)
                    if log_channel is None:
                        log_channel = await bot.fetch_channel(TRANSFER_LOG_CHANNEL_ID)
                    copied_embeds = [discord.Embed.from_dict(e.to_dict()) for e in message.embeds[:10]]
                    safe_content = discord.utils.escape_mentions(message.content or "")
                    header = f"📨 **송금 내역 전달** · [원본 메시지]({message.jump_url})"
                    target = await log_channel.send(
                        content=header + ("\n" + safe_content[:1500] if safe_content else ""),
                        embeds=copied_embeds,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                # Send a regular channel message (not a reply) below the transfer record.
                await target.channel.send(
                    f"💰 <@{recipient_id}>님에게 **{amount:,}원**이 송금되었습니다!",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=recipient_id)], roles=False, everyone=False),
                )
                _notified_transfers.add(message.id)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not forward transfer or send recipient notification: msg=%s", message.id)
            return

# Homework completion is identified only from Dodo's own interaction response.
# A successful dig alone is not sufficient: the completion-box award must be present.
# Dodo may put Discord markdown (*, _, etc.) between the gift emoji, count and suffix.
# Match the number near the award phrase without depending on the emoji or markdown.
HOMEWORK_BOX = re.compile(
    r"오늘도\s*수고\s*박스[\s\S]{0,120}?(\d[\d,]*)[\s*_~`]*개(?:를|을)?"
)
HOMEWORK_DONE = re.compile(r"오늘의\s*모든\s*할\s*일을\s*다\s*했구나")


def homework_result(message: discord.Message):
    """Return (actor_id, box_count) only for a verified Dodo completion award."""
    if message.guild is None or message.guild.id != OWNER_GUILD_ID:
        return None
    if not DODO_BOT_ID:
        log.warning("Homework disabled: Railway DODO_BOT_ID is missing")
        return None
    if message.author.id != DODO_BOT_ID:
        return None
    texts = [message.content or ""]
    for embed in message.embeds:
        texts.extend([embed.title or "", embed.description or ""])
        for field in embed.fields:
            texts.extend([field.name or "", field.value or ""])
    body = "\n".join(texts)
    if "오늘도수고박스" not in body and not re.search(r"오늘도\s*수고\s*박스", body):
        return None
    if "땅파기 성공" not in body or not HOMEWORK_DONE.search(body):
        log.warning("Homework award but missing success/completion proof: message=%s body=%r", message.id, body[:800])
        return None
    match = HOMEWORK_BOX.search(body)
    if not match:
        log.warning("Homework box count not parsed: message=%s body=%r", message.id, body[:800])
        return None
    meta = getattr(message, "interaction_metadata", None) or getattr(message, "interaction", None)
    user = getattr(meta, "user", None) if meta is not None else None
    command_name = getattr(meta, "name", None)
    if user is None:
        log.warning("Homework award found but interaction user unavailable: message=%s metadata_type=%s", message.id, type(meta).__name__)
        return None
    if command_name and "땅파기" not in command_name:
        log.warning("Homework unexpected interaction command: message=%s command=%s", message.id, command_name)
        return None
    count = int(match.group(1).replace(",", ""))
    if count < 1 or count > 999:
        log.warning("Homework invalid box count: message=%s count=%s", message.id, count)
        return None
    log.info("Homework completion matched: message=%s user=%s boxes=%s", message.id, user.id, count)
    return user.id, count


async def send_homework_webhook(channel, member, embed):
    """Send a homework embed with the member's display identity, no ping."""
    webhooks = await channel.webhooks()
    webhook = next(
        (hook for hook in webhooks
         if hook.name == "도도 숙제 자동인증"
         and hook.user is not None and bot.user is not None
         and hook.user.id == bot.user.id and hook.token),
        None,
    )
    if webhook is None:
        webhook = await channel.create_webhook(
            name="도도 숙제 자동인증",
            reason="숙제 완료 인증을 유저 이름과 프로필로 게시",
        )
    display_name = member.display_name[:80] or str(member.id)
    # Prefer the member's server avatar, then their global user avatar.
    # Fetching the user also handles an incomplete member object in cache.
    avatar_asset = member.display_avatar
    if avatar_asset is None:
        try:
            fetched_user = await bot.fetch_user(member.id)
            avatar_asset = fetched_user.avatar
        except discord.HTTPException:
            log.warning('Could not fetch global avatar for user=%s', member.id)
    if avatar_asset is None:
        log.warning('No custom avatar found for user=%s; using default avatar', member.id)
        avatar_asset = member.default_avatar
    avatar_url = avatar_asset.replace(format='png', size=256).url if not avatar_asset.is_animated() else avatar_asset.replace(format='gif', size=256).url
    log.info('Homework webhook avatar: user=%s custom=%s url=%s',
             member.id, bool(member.guild_avatar or member.avatar), avatar_url)
    return await webhook.send(
        username=display_name,
        avatar_url=avatar_url,
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True,
    )


@bot.tree.command(name='숙제인증테스트', description='관리자 전용: 숙제 완료 웹후크 인증을 시험합니다.')
@app_commands.describe(유저='테스트 인증에 표시할 유저', 수고박스='가상 지급 개수 (1~999)')
@app_commands.default_permissions(administrator=True)
async def homework_test(
    interaction: discord.Interaction,
    유저: discord.Member,
    수고박스: app_commands.Range[int, 1, 999],
):
    if interaction.guild_id != OWNER_GUILD_ID or not interaction.guild:
        await interaction.response.send_message('운영 서버에서만 사용할 수 있습니다.', ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('서버 관리자만 사용할 수 있습니다.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        channel = interaction.guild.get_channel(HOMEWORK_CHANNEL_ID) or await bot.fetch_channel(HOMEWORK_CHANNEL_ID)
        day = datetime.now(KST).date()
        embed = discord.Embed(
            title=f'🧪 테스트 인증 — {day.month}월 {day.day}일 숙제 완료 인증',
            description=(f'<@{유저.id}> 님이 오늘의 숙제를 모두 완료했습니다!\n\n'
                         f'🎁 오늘도수고박스 **{수고박스:,}개 지급**\n\n'
                         '※ 테스트 메시지 · 실제 완료 기록에 반영되지 않음'),
            color=0x36B37E,
        )
        msg = await send_homework_webhook(channel, 유저, embed)
        await interaction.followup.send(
            f'테스트 인증을 게시했어. 확인 후 메시지를 삭제하면 돼.\n{msg.jump_url}',
            ephemeral=True,
        )
    except (discord.Forbidden, discord.HTTPException):
        log.exception('Homework test failed (permissions or Discord API)')
        await interaction.followup.send('전송 실패: 인증 채널의 웹후크 관리·채널 보기·메시지 보내기 권한을 확인해 줘.', ephemeral=True)
    except Exception:
        log.exception('Homework test unexpected error')
        await interaction.followup.send('테스트 중 오류가 발생했어. Railway 로그를 확인해 줘.', ephemeral=True)


@bot.tree.command(name='숙제인증재검사', description='관리자 전용: 기존 도도 숙제 완료 메시지를 다시 검사합니다.')
@app_commands.describe(메시지링크='도도봇의 땅파기 성공 메시지 링크')
@app_commands.default_permissions(administrator=True)
async def homework_recheck(interaction: discord.Interaction, 메시지링크: str):
    if interaction.guild_id != OWNER_GUILD_ID or not interaction.guild:
        await interaction.response.send_message('운영 서버에서만 사용할 수 있습니다.', ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('서버 관리자만 사용할 수 있습니다.', ephemeral=True)
        return
    match = re.fullmatch(r'https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)', 메시지링크.strip())
    if not match or int(match.group(1)) != OWNER_GUILD_ID:
        await interaction.response.send_message('운영 서버의 도도봇 메시지 링크를 입력해 줘.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        channel = interaction.guild.get_channel(int(match.group(2))) or await bot.fetch_channel(int(match.group(2)))
        source = await channel.fetch_message(int(match.group(3)))
        result = homework_result(source)
        if result is None:
            await interaction.followup.send('인증 조건을 확인하지 못했어. Railway 로그에서 Homework 항목을 확인해 줘.', ephemeral=True)
            return
        await announce_homework(source)
        await interaction.followup.send(f'재검사 완료: <@{result[0]}> · 수고박스 {result[1]}개. 이미 인증한 날짜라면 중복 전송하지 않아.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.exception('Homework recheck cannot fetch message')
        await interaction.followup.send('원본 메시지를 읽을 수 없어. 봇의 채널 보기·메시지 기록 읽기 권한을 확인해 줘.', ephemeral=True)
    except Exception:
        log.exception('Homework recheck failed')
        await interaction.followup.send('재검사 중 오류가 발생했어. Railway 로그를 확인해 줘.', ephemeral=True)


async def announce_homework(message: discord.Message):
    result = homework_result(message)
    if result is None:
        return
    user_id, box_count = result
    # The day is the actual Dodo response timestamp in Korean local time.
    day = message.created_at.astimezone(KST).date()
    try:
        async with pool.acquire() as conn:
            claimed = await conn.fetchval(
                """INSERT INTO homework_announcements(guild_id,user_id,day,source_message_id)
                   VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING user_id""",
                OWNER_GUILD_ID, user_id, day, message.id,
            )
        if claimed is None:
            return
    except Exception:
        log.exception("Homework deduplication database error: message=%s", message.id)
        return
    try:
        channel = message.guild.get_channel(HOMEWORK_CHANNEL_ID) or await bot.fetch_channel(HOMEWORK_CHANNEL_ID)
        member = message.guild.get_member(user_id)
        if member is None:
            try:
                member = await message.guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden):
                member = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title=f"✅ {day.month}월 {day.day}일 숙제 완료 인증",
            description=(f"<@{user_id}> 님이 오늘의 숙제를 모두 완료했습니다!\n\n"
                         f"🎁 오늘도수고박스 **{box_count:,}개 지급**"),
            color=0x36B37E,
        )
        await send_homework_webhook(channel, member, embed)
        log.info("Homework certified: user=%s date=%s boxes=%s", user_id, day, box_count)
    except Exception:
        # A failed send must not permanently consume today's one announcement.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM homework_announcements WHERE guild_id=$1 AND user_id=$2 AND day=$3 AND source_message_id=$4",
                OWNER_GUILD_ID, user_id, day, message.id,
            )
        log.exception("Homework announcement failed: message=%s", message.id)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    await notify_transfer_recipient(after)
    await announce_homework(after)

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    # Cached edits are already handled above; fetch uncached edited messages.
    if payload.cached_message is not None:
        return
    try:
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        await notify_transfer_recipient(message)
        await announce_homework(message)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        log.exception("Could not inspect edited Dodo message")

@bot.event
async def on_message(message:discord.Message):
    if message.guild is None:
        return
    if message.author.id == DODO_BOT_ID:
        await notify_transfer_recipient(message)
        await announce_homework(message)
        return
    if message.author.bot:
        return
    # Restrict ID lookup to two channels in the owner server only.
    # Other servers may use ID lookup in any accessible channel.
    if message.guild.id == OWNER_GUILD_ID and message.channel.id not in OWNER_ID_CHANNEL_IDS:
        return
    match=MENTION_ONLY.fullmatch(message.content.strip())
    if not match or len(message.mentions)!=1 or message.mention_everyone or message.role_mentions:return
    if int(match.group(1))!=message.mentions[0].id:return
    await message.reply(match.group(1), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

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
        # Register slash commands only in the owner's guild.
        guild = discord.Object(id=OWNER_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()  # Remove any previously published global commands.
        await bot.tree.sync(guild=guild)
        expiry_loop.start()
        await bot.connect()

if __name__=='__main__':
    asyncio.run(main())
