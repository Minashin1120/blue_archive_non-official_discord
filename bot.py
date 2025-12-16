import discord
import os
import asyncio
import datetime
import aiomysql
from aiohttp import web
from collections import defaultdict, deque
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# --- 環境変数 ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
WELCOME_CHANNEL_ID = int(os.getenv('WELCOME_CHANNEL_ID'))
RULES_CHANNEL_ID = int(os.getenv('RULES_CHANNEL_ID'))
NEW_ROLE_ID = int(os.getenv('NEW_ROLE_ID'))
PORT = 3120

# 固定ID設定
COMMAND_ADMIN_ROLE_ID = 1443565021509586990
NOTIFY_CHANNEL_ID = 1443574124638375937
NEW_USER_ROLE_ID = 1443870588845297694
STATUS_CHANNEL_ID = 1443565022675468382

# Bot管理者ID (攻撃検知通知用) - 前回の設定を引き継ぐ必要があるため、実行時にsedで置換するか、
# 環境変数から読み込むのがベストだが、ここではハードコード箇所としてプレースホルダーにしておく
# 実際には前回のスクリプト実行時の値が入っているはずだが、上書きするため再設定が必要になる可能性がある
# 簡易的に環境変数または固定値を使う設計にする
BOT_ADMIN_USER_ID = 0  # メンション用ID（必要なら書き換えてください）

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

# --- 署名追加関数 ---
def add_signature(embed: discord.Embed):
    """EmbedのDescription末尾に製作者情報とリポジトリリンクを追加する"""
    signature = (
        "\n\n───────────────\n"
        "🛠️ Dev: [Minashin1120](https://x.com/Minashin1120) | 📦 [Repository](https://github.com/Minashin1120/blue_archive_non-official_discord)\n"
        "🔒 *This bot is developed exclusively for this server.*"
    )
    
    if embed.description is None or embed.description is discord.Embed.Empty:
        embed.description = signature
    else:
        embed.description += signature
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
        
        if now < self.global_unlock_time:
            return "GLOBAL_LOCKED"

        if user_id in self.locked_users:
            if now < self.locked_users[user_id]:
                return "USER_LOCKED"
            else:
                del self.locked_users[user_id]
        
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
        if not self.pool:
            self.pool = await aiomysql.create_pool(**DB_CONFIG)
        self.add_view(RulesView(self))
        try:
            await self.tree.sync()
            print("Slash commands synced.")
        except Exception as e:
            print(f"Command sync failed: {e}")

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
        await super().close()

bot = MyBot()

# --- DB操作ヘルパー ---
async def execute_db(query, args=None):
    if not bot.pool: return None
    async with bot.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, args)
            return await cur.fetchone()

async def get_setting(key):
    res = await execute_db("SELECT value FROM settings WHERE key_name = %s", (key,))
    return res[0] if res else None

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
        add_signature(embed) # 署名追加
        await channel.send(content=admin_mention, embed=embed)
    except Exception as e:
        print(f"Failed to send attack alert: {e}")

async def send_user_alert(member, channel, alert_type="add", is_manual=False):
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
        
        add_signature(embed) # 署名追加
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send notification: {e}")

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
            add_signature(embed) # 署名追加
            msg = await channel.send(embed=embed)
            await set_setting('lockout_msg_id', msg.id)
        except: pass
    await bot.close()

# --- View ---
class RulesView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance

    @discord.ui.button(label="ルールに同意して参加", style=discord.ButtonStyle.green, custom_id="agree_rules_button")
    async def agree_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        status = rate_limiter.check_action(interaction.user.id)
        
        if status == "TRIGGER_GLOBAL_LOCK":
            await send_attack_alert(interaction.user.id, "集団攻撃トリガー (システム緊急停止)")
            await interaction.response.send_message("🚨 異常検知。システムを緊急停止します。", ephemeral=True)
            await trigger_emergency_shutdown()
            return
        elif status == "GLOBAL_LOCKED":
            return
        elif status == "TRIGGER_USER_LOCK":
            await send_attack_alert(interaction.user.id, "個人スパム検知 (5分間ブロック)")
            await interaction.response.send_message("⚠️ 操作が速すぎます。5分間ブロックします。", ephemeral=True)
            return
        elif status == "USER_LOCKED":
            await interaction.response.send_message("⚠️ 操作制限中です。", ephemeral=True)
            return

        role_id = NEW_ROLE_ID
        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("エラー: 設定エラー。", ephemeral=True)
            return
        
        if role in interaction.user.roles:
            try:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message("ルールに同意しました。ようこそ！", ephemeral=True)
                
                notify_channel = self.bot.get_channel(NOTIFY_CHANNEL_ID)
                if notify_channel:
                    embed = discord.Embed(title="✅ ルール同意", description=f"{interaction.user.mention} がルールに同意し、参加しました。", color=0x2ecc71)
                    if interaction.user.display_avatar: embed.set_thumbnail(url=interaction.user.display_avatar.url)
                    embed.set_footer(text=f"User ID: {interaction.user.id}")
                    embed.timestamp = datetime.datetime.now()
                    add_signature(embed) # 署名追加
                    await notify_channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.response.send_message("エラー: 権限不足", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"エラー: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("既に同意済みです。", ephemeral=True)

# --- イベント & タスク ---
@tasks.loop(minutes=60)
async def check_new_users_task():
    try:
        val = await get_setting('threshold_days')
        threshold = int(val) if val else 7
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

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    if not check_new_users_task.is_running():
        check_new_users_task.start()
    
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
    r = member.guild.get_role(NEW_ROLE_ID)
    if r:
        try: await member.add_roles(r)
        except: pass
    
    try:
        val = await get_setting('threshold_days')
        threshold = int(val) if val else 7
        now = datetime.datetime.now(datetime.timezone.utc)
        if member.created_at and (now - member.created_at).days < threshold:
            nr = member.guild.get_role(NEW_USER_ROLE_ID)
            nc = bot.get_channel(NOTIFY_CHANNEL_ID)
            if nr:
                await member.add_roles(nr)
                await send_user_alert(member, nc, "add", False)
    except: pass
    
    wc = bot.get_channel(WELCOME_CHANNEL_ID)
    rc = bot.get_channel(RULES_CHANNEL_ID)
    if wc and rc:
        e = discord.Embed(title=f"ようこそ {member.name} さん！", description=f"{rc.mention} を確認してください。", color=0x00ff00)
        add_signature(e) # 署名追加
        try: await wc.send(content=member.mention, embed=e)
        except: pass

@bot.event
async def on_member_remove(member):
    c = bot.get_channel(NOTIFY_CHANNEL_ID)
    if c:
        e = discord.Embed(title="👋 メンバー退出", description=f"{member.mention} 退出", color=0x95a5a6)
        e.set_footer(text=f"ID: {member.id}"); e.timestamp = datetime.datetime.now()
        add_signature(e) # 署名追加
        try: await c.send(embed=e)
        except: pass

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
    add_signature(e) # 署名追加
    try: await c.send(embed=e)
    except: pass

# --- コマンド ---
def is_admin_or_has_role():
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator: return True
        if isinstance(interaction.user, discord.Member):
            if any(role.id == COMMAND_ADMIN_ROLE_ID for role in interaction.user.roles): return True
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

@bot.tree.command(name="ping", description="Ping")
async def ping(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"応答速度: {latency}ms", color=0x3498db)
    add_signature(embed)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="scan_users", description="手動スキャン")
@is_admin_or_has_role()
async def scan_users(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    await interaction.response.send_message("スキャン開始...", ephemeral=True)
    
    val = await get_setting('threshold_days')
    threshold = int(val) if val else 7
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
    await interaction.followup.send(f"完了: +{cnt_add} / -{cnt_rem}", ephemeral=True)

@bot.tree.command(name="deploy_rules", description="パネル設置")
@is_admin_or_has_role()
async def deploy_rules(interaction: discord.Interaction):
    if not await check_rate_limit(interaction): return
    e = discord.Embed(title="📜 ルール同意", description="確認してボタンを押してください。", color=0xff0000)
    add_signature(e) # 署名追加
    await interaction.channel.send(embed=e, view=RulesView(bot))
    await interaction.response.send_message("設置完了", ephemeral=True)

@bot.tree.command(name="set_new_account_days", description="期間設定")
@is_admin_or_has_role()
async def set_new_account_days(interaction: discord.Interaction, days: int):
    if not await check_rate_limit(interaction): return
    await set_setting('threshold_days', days)
    await interaction.response.send_message(f"設定更新: {days}日", ephemeral=True)

@deploy_rules.error
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
