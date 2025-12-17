import discord
import os
import asyncio
import datetime
import aiomysql
import json
import jwt
import aiohttp
from aiohttp import web
from collections import defaultdict, deque
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# --- 環境変数 & 設定 ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
PORT = 3120

# Cloudflare & Security (キー名を.envに合わせる)
CF_SITE_KEY = os.getenv('CF_TURNSTILE_SITE_KEY')
CF_SECRET_KEY = os.getenv('CF_TURNSTILE_SECRET_KEY')
JWT_SECRET = os.getenv('JWT_SECRET')
BASE_URL = "https://buruaka-min-shugo-welcome-bot.minashin1120.com"

# ロールID
UNVERIFIED_ROLE_ID = 1443590180396335174 # 未認証
VERIFIED_ROLE_ID = 1450301488697049170   # 認証済み
MODERATOR_ROLE_ID = 1450755263152914432  # モデレーター
NEW_USER_ROLE_ID = 1443870588845297694   # 新規ユーザー

# チャンネルID
WELCOME_CHANNEL_ID = 1443565022675468379 # 入室
RULES_CHANNEL_ID = int(os.getenv('RULES_CHANNEL_ID', '0'))
NOTIFY_CHANNEL_ID = 1443574124638375937  # ログ
STATUS_CHANNEL_ID = 1443565022675468382  # 緊急通知

# Bot管理者ID
BOT_ADMIN_USER_ID = 0
COMMAND_ADMIN_ROLE_ID = 1443565021509586990

# DB設定
DB_CONFIG = {
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'db': os.getenv('DB_NAME'),
    'autocommit': True
}

# 負荷対策設定
RATE_LIMIT_ACTIONS = 5
RATE_LIMIT_WINDOW = 10
USER_LOCKOUT_TIME = 300
GLOBAL_LOCKOUT_THRESHOLD = 5
GLOBAL_LOCKOUT_TIME = 600

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# --- 共通関数: 署名 ---
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
        if now < self.global_unlock_time: return "GLOBAL_LOCKED"
        if user_id in self.locked_users:
            if now < self.locked_users[user_id]: return "USER_LOCKED"
            else: del self.locked_users[user_id]
        
        history = self.user_history[user_id]
        history.append(now)
        if len(history) == RATE_LIMIT_ACTIONS:
            if now - history[0] < RATE_LIMIT_WINDOW:
                self.locked_users[user_id] = now + USER_LOCKOUT_TIME
                active_locks = sum(1 for t in self.locked_users.values() if t > now)
                if active_locks >= GLOBAL_LOCKOUT_THRESHOLD:
                    self.global_unlock_time = now + GLOBAL_LOCKOUT_TIME
                    return "TRIGGER_GLOBAL_LOCK"
                return "TRIGGER_USER_LOCK"
        return "OK"

rate_limiter = RateLimiter()

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.pool = None

    async def setup_hook(self):
        # DB接続
        if not self.pool:
            self.pool = await aiomysql.create_pool(**DB_CONFIG)
        
        # View登録
        self.add_view(RulesView(self))
        
        # コマンド同期 (Discordキャッシュ対策: 毎回同期)
        try:
            print("Syncing slash commands...")
            await self.tree.sync()
            print("Slash commands synced successfully.")
        except Exception as e:
            print(f"Command sync failed: {e}")

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
        await super().close()

bot = MyBot()

# --- DB操作ヘルパー ---
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
            await cur.execute(
                "INSERT INTO settings (key_name, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = %s",
                (key, str(value), str(value))
            )

async def delete_setting(key):
    async with bot.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM settings WHERE key_name = %s", (key,))

# --- 通知系関数 ---
async def send_attack_alert(user_id, attack_type):
    channel = bot.get_channel(NOTIFY_CHANNEL_ID)
    if not channel: return
    try:
        admin_mention = f"<@{BOT_ADMIN_USER_ID}>" if BOT_ADMIN_USER_ID else ""
        embed = discord.Embed(title="🚨 攻撃/スパム検知", color=0xff0000)
        embed.description = "レートリミットに抵触する過剰な操作を検知しました。"
        embed.add_field(name="実行者", value=f"<@{user_id}> (ID: {user_id})", inline=False)
        embed.add_field(name="検知内容", value=attack_type, inline=False)
        embed.timestamp = datetime.datetime.now()
        add_signature(embed)
        await channel.send(content=admin_mention, embed=embed)
    except Exception as e:
        print(f"Failed to send attack alert: {e}")

async def trigger_emergency_shutdown():
    print("!!! GLOBAL LOCKOUT TRIGGERED - SHUTTING DOWN !!!")
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel:
        try:
            embed = discord.Embed(
                title="🚨 緊急停止通知",
                description=f"集団攻撃を検知したため、システムを約 {GLOBAL_LOCKOUT_TIME // 60} 分間停止します。",
                color=0xff0000
            )
            embed.timestamp = datetime.datetime.now()
            add_signature(embed)
            msg = await channel.send(embed=embed)
            await set_setting('lockout_msg_id', msg.id)
        except: pass
    await bot.close()

async def send_user_alert(member, channel, alert_type="add", is_manual=False):
    """新規ユーザー・ロール変更通知"""
    if not channel: return
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        if member.created_at:
            diff = now - member.created_at
            days = diff.days
            hours = diff.seconds // 3600
        else: days=0; hours=0
        
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
    except Exception as e:
        print(f"Failed to send notification: {e}")

async def approve_user(user, guild):
    """認証完了処理"""
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
                embed.set_footer(text=f"ID: {user.id}")
                embed.timestamp = datetime.datetime.now()
                add_signature(embed)
                await nc.send(embed=embed)
            return True
    except Exception as e:
        print(f"Approval error: {e}")
    return False

# --- 権限チェック ---
def is_moderator():
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator: return True
        if isinstance(interaction.user, discord.Member):
            if any(role.id == MODERATOR_ROLE_ID for role in interaction.user.roles): return True
        return False
    return app_commands.check(predicate)

async def check_rate_limit(interaction: discord.Interaction):
    status = rate_limiter.check_action(interaction.user.id)
    if status == "TRIGGER_GLOBAL_LOCK":
        await send_attack_alert(interaction.user.id, "集団攻撃トリガー (システム緊急停止)")
        await interaction.response.send_message("🚨 異常検知。システム停止。", ephemeral=True)
        await trigger_emergency_shutdown()
        return False
    elif status == "GLOBAL_LOCKED":
        return False
    elif status == "TRIGGER_USER_LOCK":
        await send_attack_alert(interaction.user.id, "個人スパム検知 (5分間ブロック)")
        await interaction.response.send_message("⚠️ 操作過多。ブロックします。", ephemeral=True)
        return False
    elif status == "USER_LOCKED":
        await interaction.response.send_message("⚠️ 制限中です。", ephemeral=True)
        return False
    return True

# --- View (同意ボタン) ---
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
            # JWT生成
            payload = {'user_id': interaction.user.id, 'guild_id': interaction.guild.id, 'exp': datetime.datetime.utcnow() + datetime.timedelta(minutes=15)}
            token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
            url = f"{BASE_URL}/verify?token={token}"
            await interaction.response.send_message(f"以下のURLから認証してください。\n👉 [認証サイトへ移動]({url})", ephemeral=True)
        else:
            await approve_user(interaction.user, interaction.guild)
            await interaction.response.send_message("認証が完了しました！", ephemeral=True)

# --- 定期タスク ---
@tasks.loop(minutes=60)
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
        await channel.send(msg, delete_after=3600)
    except: pass

@tasks.loop(minutes=60)
async def check_new_users_expiry():
    try:
        val = await get_setting('threshold_days', '7')
        threshold = int(val)
        for guild in bot.guilds:
            role = guild.get_role(NEW_USER_ROLE_ID)
            nc = bot.get_channel(NOTIFY_CHANNEL_ID)
            if not role: continue
            for member in role.members:
                now = datetime.datetime.now(datetime.timezone.utc)
                if member.created_at and (now - member.created_at).days >= threshold:
                    try:
                        await member.remove_roles(role)
                        await send_user_alert(member, nc, "remove", False)
                    except: pass
    except: pass

# --- イベント ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    if not poke_unverified_users.is_running(): poke_unverified_users.start()
    if not check_new_users_expiry.is_running(): check_new_users_expiry.start()
    
    # 復帰処理
    try:
        msg_id_str = await get_setting('lockout_msg_id')
        if msg_id_str:
            msg_id = int(msg_id_str)
            sc = bot.get_channel(STATUS_CHANNEL_ID)
            if sc:
                try:
                    msg = await sc.fetch_message(msg_id)
                    await msg.delete()
                except: pass
                embed = discord.Embed(title="✅ システム復帰", description="システムが再起動し、正常稼働に戻りました。", color=0x2ecc71)
                add_signature(embed)
                await sc.send(embed=embed, delete_after=30)
            await delete_setting('lockout_msg_id')
    except: pass

@bot.event
async def on_member_join(member):
    if member.bot: return
    guild = member.guild
    
    # 1. ロール復元
    restored = False
    try:
        async with bot.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT role_ids FROM user_role_backups WHERE user_id = %s", (member.id,))
                res = await cur.fetchone()
                if res:
                    role_ids = json.loads(res[0])
                    roles_to_add = []
                    for rid in role_ids:
                        r = guild.get_role(rid)
                        if r and not r.is_default() and not r.managed and r < guild.me.top_role:
                            roles_to_add.append(r)
                    if roles_to_add:
                        await member.add_roles(*roles_to_add)
                        restored = True
    except: pass

    # 2. 未認証ロール付与 (認証済みでなければ)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    if not (restored and verified_role in member.roles):
        unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
        if unverified_role:
            try: await member.add_roles(unverified_role)
            except: pass

    # 3. 新規アカウントチェック
    try:
        val = await get_setting('threshold_days', '7')
        threshold = int(val)
        now = datetime.datetime.now(datetime.timezone.utc)
        if member.created_at and (now - member.created_at).days < threshold:
            new_user_role = guild.get_role(NEW_USER_ROLE_ID)
            nc = bot.get_channel(NOTIFY_CHANNEL_ID)
            if new_user_role:
                await member.add_roles(new_user_role)
                await send_user_alert(member, nc, "add", False)
    except: pass

    # 4. ウェルカムメッセージ
    wc = guild.get_channel(WELCOME_CHANNEL_ID)
    rc = guild.get_channel(RULES_CHANNEL_ID)
    if wc and rc:
        desc = f"ようこそ {member.mention} さん！\n\n{rc.mention} を確認し、認証を行ってください。"
        if restored: desc += "\n\n🔄 **以前のロール設定を復元しました。**"
        desc += "\n\n⚠️ **同意するまで他のチャンネルは閲覧できません。**"
        
        embed = discord.Embed(title="🎉 サーバーへようこそ", description=desc, color=0x00ff00)
        add_signature(embed)
        await wc.send(content=member.mention, embed=embed)

@bot.event
async def on_member_remove(member):
    if member.bot: return
    try:
        role_ids = [r.id for r in member.roles if not r.is_default() and not r.managed]
        if role_ids:
            async with bot.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("INSERT INTO user_role_backups (user_id, role_ids) VALUES (%s, %s) ON DUPLICATE KEY UPDATE role_ids = %s", (member.id, json.dumps(role_ids), json.dumps(role_ids)))
    except: pass
    nc = bot.get_channel(NOTIFY_CHANNEL_ID)
    if nc:
        embed = discord.Embed(title="👋 メンバー退出", description=f"{member.mention} ({member.name})", color=0x95a5a6)
        embed.set_footer(text=f"ID: {member.id}"); embed.timestamp = datetime.datetime.now()
        add_signature(embed)
        await nc.send(embed=embed)

@bot.event
async def on_member_update(before, after):
    if before.roles == after.roles: return
    c = bot.get_channel(NOTIFY_CHANNEL_ID)
    if not c: return
    added = set(after.roles) - set(before.roles)
    removed = set(before.roles) - set(after.roles)
    if not added and not removed: return
    e = discord.Embed(title="🔄 ロール更新ログ", color=0xf1c40f)
    e.description = f"{after.mention} ロール変更"
    if after.display_avatar: e.set_thumbnail(url=after.display_avatar.url)
    if added: e.add_field(name="➕ 付与", value=", ".join([r.mention for r in added]))
    if removed: e.add_field(name="➖ 解除", value=", ".join([r.mention for r in removed]))
    e.timestamp = datetime.datetime.now()
    add_signature(e)
    try: await c.send(embed=e)
    except: pass

# --- コマンド定義 ---
@bot.tree.command(name="ping", description="Ping")
async def ping(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"応答速度: {latency}ms", color=0x3498db)
    add_signature(embed)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="deploy_rules", description="認証パネル設置")
@is_moderator()
async def deploy_rules(interaction: discord.Interaction):
    embed = discord.Embed(title="🛡️ サーバー認証", description="下のボタンを押して認証プロセスに進んでください。", color=0x3498db)
    add_signature(embed)
    await interaction.channel.send(embed=embed, view=RulesView(bot))
    await interaction.response.send_message("設置完了", ephemeral=True)

@bot.tree.command(name="toggle_verification", description="認証モード切替")
@is_moderator()
@app_commands.choices(mode=[app_commands.Choice(name="Web", value="web"), app_commands.Choice(name="Button", value="button")])
async def toggle_verification(interaction: discord.Interaction, mode: str):
    await set_setting('verification_mode', mode)
    await interaction.response.send_message(f"モード変更: {mode}", ephemeral=True)

@bot.tree.command(name="revoke_verification", description="認証取消")
@is_moderator()
async def revoke_verification(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        roles_to_remove = [r for r in target.roles if not r.is_default() and not r.managed and r < interaction.guild.me.top_role]
        if roles_to_remove: await target.remove_roles(*roles_to_remove)
        unverified = interaction.guild.get_role(UNVERIFIED_ROLE_ID)
        if unverified: await target.add_roles(unverified)
        await interaction.followup.send(f"{target.mention} の認証を取り消しました。")
    except Exception as e: await interaction.followup.send(f"エラー: {e}")

@bot.tree.command(name="set_new_account_days", description="新規判定日数")
@is_moderator()
async def set_new_account_days(interaction: discord.Interaction, days: int):
    await set_setting('threshold_days', days)
    await interaction.response.send_message(f"設定更新: {days}日", ephemeral=True)

@bot.tree.command(name="scan_users", description="手動スキャン")
@is_moderator()
async def scan_users(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    await interaction.response.send_message("スキャン中...", ephemeral=True)
    val = await get_setting('threshold_days', '7'); threshold = int(val)
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
                    await m.add_roles(role_new)
                    cnt_add+=1
                    await send_user_alert(m, nc, "add", True)
                    await asyncio.sleep(0.5)
                except: pass
        else:
            if role_new in m.roles:
                try:
                    await m.remove_roles(role_new)
                    cnt_rem+=1
                    await send_user_alert(m, nc, "remove", True)
                    await asyncio.sleep(0.5)
                except: pass
    await interaction.followup.send(f"完了 (+{cnt_add}/-{cnt_rem})", ephemeral=True)

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

# --- Webサーバー & メインループ ---
async def verify_page(request):
    token = request.query.get('token')
    if not token: return web.Response(text="Token missing", status=400)
    try:
        with open(f"{os.getcwd()}/templates/verify.html", "r") as f:
            html = f.read()
        return web.Response(text=html.replace("{token}", token).replace("{site_key}", CF_SITE_KEY), content_type='text/html')
    except: return web.Response(text="Template missing", status=500)

async def verify_submit(request):
    data = await request.post()
    token = data.get('token')
    cf_response = data.get('cf-turnstile-response')
    if not token or not cf_response: return web.Response(text="Invalid Request", status=400)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        user_id = payload['user_id']
        guild_id = payload['guild_id']
    except: return web.Response(text="Invalid Token", status=403)
        
    async with aiohttp.ClientSession() as session:
        async with session.post('https://challenges.cloudflare.com/turnstile/v0/siteverify', data={
            'secret': CF_SECRET_KEY, 'response': cf_response
        }) as resp:
            result = await resp.json()
            if not result.get('success'): return web.Response(text="Captcha Failed", status=403)

    guild = bot.get_guild(guild_id)
    if guild:
        member = guild.get_member(user_id)
        if member:
            await approve_user(member, guild)
            try:
                with open(f"{os.getcwd()}/templates/success.html", "r") as f:
                    return web.Response(text=f.read(), content_type='text/html')
            except: return web.Response(text="Success", status=200)
    return web.Response(text="Error", status=404)

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/verify', verify_page), web.post('/verify/submit', verify_submit), web.get('/', lambda r: web.Response(text="Bot OK"))])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def main():
    asyncio.create_task(start_web_server())
    while True:
        try:
            print("Connecting...")
            await bot.start(TOKEN)
        except Exception as e:
            print(f"Closed: {e}")
        
        if rate_limiter.is_globally_locked():
            wt = rate_limiter.global_unlock_time - datetime.datetime.now().timestamp()
            if wt > 0:
                print(f"Global lockout: Sleeping {wt:.1f}s")
                await asyncio.sleep(wt)
            else: await asyncio.sleep(5)
        else: await asyncio.sleep(5)

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
