import discord
import os
import asyncio
import datetime
import aiomysql
import json
import jwt
import aiohttp
import random
from aiohttp import web
from collections import defaultdict, deque
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# --- 環境変数 & 設定 ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
PORT = 3120

CF_SITE_KEY = os.getenv('CF_TURNSTILE_SITE_KEY')
CF_SECRET_KEY = os.getenv('CF_TURNSTILE_SECRET_KEY')
JWT_SECRET = os.getenv('JWT_SECRET')
BASE_URL = "https://buruaka-min-shugo-welcome-bot.minashin1120.com"

UNVERIFIED_ROLE_ID = 1443590180396335174
VERIFIED_ROLE_ID = 1450301488697049170
MODERATOR_ROLE_ID = 1450755263152914432
NEW_USER_ROLE_ID = 1443870588845297694

WELCOME_CHANNEL_ID = 1443565022675468379
RULES_CHANNEL_ID = int(os.getenv('RULES_CHANNEL_ID', '0'))
NOTIFY_CHANNEL_ID = 1443574124638375937
STATUS_CHANNEL_ID = 1443565022675468382

BOT_ADMIN_USER_ID = 0
COMMAND_ADMIN_ROLE_ID = 1443565021509586990

DB_CONFIG = {
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'db': os.getenv('DB_NAME'),
    'autocommit': True
}

# --- 負荷対策設定 ---
RATE_LIMIT_ACTIONS = 5
RATE_LIMIT_WINDOW = 10
USER_LOCKOUT_MIN = 300
USER_LOCKOUT_MAX = 600
GLOBAL_LOCKOUT_THRESHOLD = 5
GLOBAL_LOCKOUT_MIN = 600
GLOBAL_LOCKOUT_MAX = 1800

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# --- 共通関数 ---
def add_signature(embed: discord.Embed):
    signature = (
        "\n\n───────────────\n"
        "🛠️ Dev: [Minashin1120](https://x.com/Minashin1120) | 📦 [Repository](https://github.com/Minashin1120/blue_archive_non-official_discord)\n"
        "🔒 *This bot is developed exclusively for this server.*"
    )
    if embed.description is None: embed.description = signature
    else: embed.description += signature
    return embed

# --- 負荷対策クラス ---
class RateLimiter:
    def __init__(self):
        self.user_history = defaultdict(lambda: deque(maxlen=RATE_LIMIT_ACTIONS))
        self.locked_users = {}
        self.global_unlock_time = 0

    def is_globally_locked(self):
        return datetime.datetime.now().timestamp() < self.global_unlock_time

    def check_action(self, user_id):
        now = datetime.datetime.now().timestamp()
        if now < self.global_unlock_time: return "GLOBAL_LOCKED", None
        if user_id in self.locked_users:
            if now < self.locked_users[user_id]: return "USER_LOCKED", None
            else: del self.locked_users[user_id]
        
        history = self.user_history[user_id]
        history.append(now)
        if len(history) == RATE_LIMIT_ACTIONS:
            if now - history[0] < RATE_LIMIT_WINDOW:
                user_duration = random.randint(USER_LOCKOUT_MIN, USER_LOCKOUT_MAX)
                self.locked_users[user_id] = now + user_duration
                active_locks = sum(1 for t in self.locked_users.values() if t > now)
                if active_locks >= GLOBAL_LOCKOUT_THRESHOLD:
                    global_duration = random.randint(GLOBAL_LOCKOUT_MIN, GLOBAL_LOCKOUT_MAX)
                    self.global_unlock_time = now + global_duration
                    return "TRIGGER_GLOBAL_LOCK", global_duration
                return "TRIGGER_USER_LOCK", user_duration
        return "OK", None

rate_limiter = RateLimiter()

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.pool = None

    async def setup_hook(self):
        if not self.pool: self.pool = await aiomysql.create_pool(**DB_CONFIG)
        self.add_view(RulesView(self))
        try: await self.tree.sync(); print("Slash commands synced.")
        except Exception as e: print(f"Command sync failed: {e}")

    async def close(self):
        if self.pool: self.pool.close(); await self.pool.wait_closed(); self.pool = None
        await super().close()

bot = MyBot()

# --- DBヘルパー ---
async def execute_db(query, args=None):
    if not bot.pool: return None
    async with bot.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, args)
            return await cur.fetchone()

async def get_setting(key, default=None):
    res = await execute_db("SELECT value FROM settings WHERE key_name = %s", (key,))
    return res[0] if res else default

async def set_setting(key, value):
    async with bot.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO settings (key_name, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = %s", (key, str(value), str(value)))

async def delete_setting(key):
    async with bot.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM settings WHERE key_name = %s", (key,))

# --- 通知処理 ---
async def send_attack_alert(user_id, attack_type, duration=None):
    channel = bot.get_channel(NOTIFY_CHANNEL_ID)
    if not channel: return
    try:
        admin_mention = f"<@{BOT_ADMIN_USER_ID}>" if BOT_ADMIN_USER_ID else ""
        embed = discord.Embed(title="🚨 攻撃/スパム検知", color=0xff0000)
        embed.description = "レートリミットに抵触する過剰な操作を検知しました。"
        embed.add_field(name="実行者", value=f"<@{user_id}> (ID: {user_id})", inline=False)
        embed.add_field(name="検知内容", value=attack_type, inline=False)
        if duration: embed.add_field(name="ロック時間(管理者用)", value=f"{duration}秒 (ランダム)", inline=False)
        embed.timestamp = datetime.datetime.now()
        add_signature(embed)
        await channel.send(content=admin_mention, embed=embed)
    except: pass

async def trigger_emergency_shutdown(duration):
    print(f"!!! GLOBAL LOCKOUT - SHUTTING DOWN FOR {duration}s !!!")
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel:
        try:
            embed = discord.Embed(title="🚨 緊急停止通知", description="集団攻撃を検知したため、システムを一時停止します。\n再開時間はセキュリティのため非公開です。", color=0xff0000)
            embed.set_footer(text=f"復帰予定: {duration}秒後 (ランダム設定)"); embed.timestamp = datetime.datetime.now()
            add_signature(embed)
            msg = await channel.send(embed=embed)
            await set_setting('lockout_msg_id', msg.id)
        except: pass
    await bot.close()

async def send_user_alert(member, channel, alert_type="add", is_manual=False):
    if not channel: return
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        days=0; hours=0
        if member.created_at:
            diff = now - member.created_at
            days = diff.days; hours = diff.seconds // 3600
        embed = discord.Embed()
        if alert_type == "add":
            embed.title = "ℹ️ 新規ユーザー検知"
            embed.color = 0x3498db
            embed.description = f"{member.mention} はアカウント作成から日が浅いため、新規ユーザーロールを付与しました。"
            embed.set_footer(text="※このロールによるサーバー機能への制限はありません")
        else:
            embed.title = "✅ 期間経過 (ロール解除)"
            embed.color = 0x2ecc71
            embed.description = f"{member.mention} はアカウント作成から期間が経過したため、新規ユーザーロールを解除しました。"
        if is_manual: embed.title += " (手動スキャン)"
        if member.display_avatar: embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="アカウント作成日", value=discord.utils.format_dt(member.created_at, style='f'), inline=False)
        embed.add_field(name="経過時間", value=f"{days}日 {hours}時間", inline=False)
        add_signature(embed)
        await channel.send(embed=embed)
    except: pass

async def approve_user(user, guild):
    unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    actions = []
    try:
        if unverified_role and unverified_role in user.roles:
            await user.remove_roles(unverified_role)
            actions.append("未認証ロール解除")
        if verified_role and verified_role not in user.roles:
            await user.add_roles(verified_role)
            actions.append("認証済みロール付与")
        if actions:
            nc = bot.get_channel(NOTIFY_CHANNEL_ID)
            if nc:
                embed = discord.Embed(title="✅ ユーザー認証完了", description=f"{user.mention} が認証されました。", color=0x2ecc71)
                embed.add_field(name="処理", value="\n".join(actions))
                embed.set_footer(text=f"ID: {user.id}"); embed.timestamp = datetime.datetime.now()
                add_signature(embed)
                await nc.send(embed=embed)
            return True
    except: pass
    return False

# --- 共通の負荷チェック ---
async def check_rate_limit(interaction: discord.Interaction):
    status, duration = rate_limiter.check_action(interaction.user.id)
    if status == "TRIGGER_GLOBAL_LOCK":
        await send_attack_alert(interaction.user.id, "集団攻撃トリガー (システム緊急停止)", duration)
        await interaction.response.send_message("🚨 異常検知。システムを緊急停止します。", ephemeral=True)
        await trigger_emergency_shutdown(duration)
        return False
    elif status == "GLOBAL_LOCKED": return False
    elif status == "TRIGGER_USER_LOCK":
        await send_attack_alert(interaction.user.id, "個人スパム検知 (ブロック)", duration)
        await interaction.response.send_message("⚠️ 操作が速すぎます。しばらくの間、操作をブロックします。", ephemeral=True)
        return False
    elif status == "USER_LOCKED":
        await interaction.response.send_message("⚠️ 操作制限中です。解除されるまでお待ちください。", ephemeral=True)
        return False
    return True

# --- View ---
class RulesView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance

    @discord.ui.button(label="認証へ進む", style=discord.ButtonStyle.blurple, custom_id="agree_rules_button")
    async def agree_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_rate_limit(interaction): return
        verified_role = interaction.guild.get_role(VERIFIED_ROLE_ID)
        if verified_role in interaction.user.roles:
            await interaction.response.send_message("✅ 既に認証済みです。", ephemeral=True)
            return
        mode = await get_setting('verification_mode', 'button')
        if mode == 'web':
            payload = {'user_id': interaction.user.id, 'guild_id': interaction.guild.id, 'exp': datetime.datetime.utcnow() + datetime.timedelta(minutes=15)}
            token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
            url = f"{BASE_URL}/verify?token={token}"
            await interaction.response.send_message(f"以下のURLから認証してください。\n👉 [認証サイトへ移動]({url})", ephemeral=True)
        else:
            await approve_user(interaction.user, interaction.guild)
            await interaction.response.send_message("認証が完了しました！", ephemeral=True)

# --- コマンド ---
def is_moderator():
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator: return True
        if isinstance(interaction.user, discord.Member):
            if any(role.id == MODERATOR_ROLE_ID for role in interaction.user.roles): return True
        return False
    return app_commands.check(predicate)

@bot.tree.command(name="ping", description="Ping")
async def ping(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"応答速度: {latency}ms", color=0x3498db)
    add_signature(embed)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="scan_users", description="手動スキャン")
@is_moderator()
async def scan_users(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    await interaction.response.send_message("スキャン開始...", ephemeral=True)
    val = await get_setting('threshold_days'); threshold = int(val) if val else 7
    role_new = interaction.guild.get_role(NEW_USER_ROLE_ID)
    nc = bot.get_channel(NOTIFY_CHANNEL_ID)
    cnt_add=0; cnt_rem=0
    now = datetime.datetime.now(datetime.timezone.utc)
    for m in interaction.guild.members:
        if m.bot or not m.created_at: continue
        age = now - m.created_at
        if age.days < threshold:
            if role_new not in m.roles:
                try:
                    await m.add_roles(role_new); cnt_add+=1
                    await send_user_alert(m, nc, "add", True); await asyncio.sleep(0.5)
                except: pass
        else:
            if role_new in m.roles:
                try:
                    await m.remove_roles(role_new); cnt_rem+=1
                    await send_user_alert(m, nc, "remove", True); await asyncio.sleep(0.5)
                except: pass
    await interaction.followup.send(f"完了 (+{cnt_add}/-{cnt_rem})", ephemeral=True)

@bot.tree.command(name="deploy_rules", description="パネル設置")
@is_moderator()
async def deploy_rules(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    e = discord.Embed(title="🛡️ サーバー認証", description="下のボタンを押して認証プロセスに進んでください。", color=0x3498db)
    add_signature(e)
    await interaction.channel.send(embed=e, view=RulesView(bot))
    await interaction.response.send_message("設置完了", ephemeral=True)

@bot.tree.command(name="toggle_verification", description="モード切替")
@is_moderator()
@app_commands.choices(mode=[app_commands.Choice(name="Web", value="web"), app_commands.Choice(name="Button", value="button")])
async def toggle_verification(interaction: discord.Interaction, mode: str):
    if not await check_rate_limit(interaction): return
    await set_setting('verification_mode', mode)
    await interaction.response.send_message(f"モード変更: {mode}", ephemeral=True)

@bot.tree.command(name="revoke_verification", description="認証取消")
@is_moderator()
async def revoke_verification(interaction: discord.Interaction, target: discord.Member):
    if not await check_rate_limit(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        roles_to_remove = [r for r in target.roles if not r.is_default() and not r.managed and r < interaction.guild.me.top_role]
        if roles_to_remove: await target.remove_roles(*roles_to_remove)
        unverified = interaction.guild.get_role(UNVERIFIED_ROLE_ID)
        if unverified: await target.add_roles(unverified)
        await interaction.followup.send(f"{target.mention} の認証を取り消しました。")
    except Exception as e: await interaction.followup.send(f"エラー: {e}")

@bot.tree.command(name="set_new_account_days", description="期間設定")
@is_moderator()
async def set_new_account_days(interaction: discord.Interaction, days: int):
    if not await check_rate_limit(interaction): return
    await set_setting('threshold_days', days)
    await interaction.response.send_message(f"設定更新: {days}日", ephemeral=True)

@deploy_rules.error
@toggle_verification.error
@revoke_verification.error
@set_new_account_days.error
@scan_users.error
@ping.error
async def command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
    else:
        try: await interaction.response.send_message(f"エラー: {error}", ephemeral=True)
        except: pass

# --- 定期タスク (ここを変更) ---
@tasks.loop(hours=72) # 3日に1回に変更
async def poke_unverified_users():
    try:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if not channel: return
        unverified_role = channel.guild.get_role(UNVERIFIED_ROLE_ID)
        if not unverified_role: return
        targets = [m for m in unverified_role.members if not m.bot]
        if not targets: return
        mentions = " ".join([m.mention for m in targets[:50]])
        if len(targets) > 50: mentions += " ..."
        rc = bot.get_channel(RULES_CHANNEL_ID)
        msg = f"{mentions}\n⚠️ **未認証のメンバーへのお知らせ**\nルールに同意し、認証を完了しないとサーバーのチャンネルを閲覧できません。\n{rc.mention if rc else '#rules'} に移動して認証を行ってください。"
        
        # 24時間 (86400秒) で削除するように変更
        await channel.send(msg, delete_after=86400)
    except: pass

@tasks.loop(minutes=60)
async def check_new_users_expiry():
    try:
        val = await get_setting('threshold_days', '7'); threshold = int(val)
        for guild in bot.guilds:
            role = guild.get_role(NEW_USER_ROLE_ID)
            nc = bot.get_channel(NOTIFY_CHANNEL_ID)
            if not role: continue
            for member in role.members:
                now = datetime.datetime.now(datetime.timezone.utc)
                if member.created_at and (now - member.created_at).days >= threshold:
                    try: await member.remove_roles(role); await send_user_alert(member, nc, "remove", False)
                    except: pass
    except: pass

# --- Webサーバー & メインループ ---
async def privacy_handler(request): return web.FileResponse(f"{os.getcwd()}/templates/privacy.html")
async def terms_handler(request): return web.FileResponse(f"{os.getcwd()}/templates/terms.html")
async def root_handler(request): return web.Response(text="Bot is running.")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', root_handler), web.get('/privacy', privacy_handler), web.get('/terms', terms_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def verify_page(request):
    token = request.query.get('token')
    if not token: return web.Response(text="Token missing", status=400)
    try:
        with open(f"{os.getcwd()}/templates/verify.html", "r") as f: html = f.read()
        return web.Response(text=html.replace("{token}", token).replace("{site_key}", CF_SITE_KEY), content_type='text/html')
    except: return web.Response(text="Template missing", status=500)

async def verify_submit(request):
    data = await request.post()
    token = data.get('token'); cf_response = data.get('cf-turnstile-response')
    if not token or not cf_response: return web.Response(text="Invalid Request", status=400)
    try: payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256']); user_id = payload['user_id']; guild_id = payload['guild_id']
    except: return web.Response(text="Invalid Token", status=403)
    async with aiohttp.ClientSession() as session:
        async with session.post('https://challenges.cloudflare.com/turnstile/v0/siteverify', data={'secret': CF_SECRET_KEY, 'response': cf_response}) as resp:
            result = await resp.json()
            if not result.get('success'): return web.Response(text="Captcha Failed", status=403)
    guild = bot.get_guild(guild_id)
    if guild:
        member = guild.get_member(user_id)
        if member:
            await approve_user(member, guild)
            try:
                with open(f"{os.getcwd()}/templates/success.html", "r") as f: return web.Response(text=f.read(), content_type='text/html')
            except: return web.Response(text="Success", status=200)
    return web.Response(text="Error", status=404)

async def main():
    app = web.Application()
    app.add_routes([web.get('/verify', verify_page), web.post('/verify/submit', verify_submit), web.get('/', root_handler), web.get('/privacy', privacy_handler), web.get('/terms', terms_handler)])
    runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, '0.0.0.0', PORT); await site.start()
    
    while True:
        try: print("Connecting..."); await bot.start(TOKEN)
        except Exception as e: print(f"Closed: {e}")
        if rate_limiter.is_globally_locked():
            wt = rate_limiter.global_unlock_time - datetime.datetime.now().timestamp()
            if wt > 0: print(f"Global lockout: Sleeping {wt:.1f}s"); await asyncio.sleep(wt)
            else: await asyncio.sleep(5)
        else: await asyncio.sleep(5)

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
