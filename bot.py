import os
import re
import sqlite3
import time
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, View
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_NAME = "stamps.db"

# 審核者身分組 ID
AUDITOR_ROLE_ID = 1527104229045436416

# 碳碳動態圖連結
CARBON_CAT_GIF_URL = (
    "https://cdn.discordapp.com/attachments/1551126968768921602/1551235720453296138/cat_look.gif"
    "?ex=6ab13c58&is=6aafead8&hm=53291691efdff6eaed417394f69895e745acfdd131e45218dad4242b69a375f3&"
)


# --- 資料庫操作（線程安全與原子操作） ---


def get_db_connection():
    """取得資料庫連線並啟用 WAL 模式提高並發穩定性"""
    conn = sqlite3.connect(DB_NAME, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stamps (
                user_id INTEGER PRIMARY KEY,
                count REAL DEFAULT 0.0
            )
            """
        )
        conn.commit()


def get_user_stamp(user_id: int) -> float:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count FROM stamps WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return round(row[0], 2) if row else 0.0


def add_user_stamp(user_id: int, amount: float) -> float:
    """原子更新使用者的印章數（相容全版本 SQLite，並限制最低為 0）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO stamps (user_id, count)
            VALUES (?, MAX(0.0, ROUND(?, 2)))
            ON CONFLICT(user_id) DO UPDATE SET
                count = MAX(0.0, ROUND(count + excluded.count, 2));
            """,
            (user_id, amount),
        )
        cursor.execute("SELECT count FROM stamps WHERE user_id = ?", (user_id,))
        new_count = cursor.fetchone()[0]
        conn.commit()
        return round(new_count, 2)


def get_leaderboard(limit: int = 10):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, count FROM stamps ORDER BY count DESC LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()


def is_auditor_or_admin(user: discord.User | discord.Member) -> bool:
    """檢查是否具有審核身分組或管理員權限"""
    if not isinstance(user, discord.Member):
        return False
    role_ids = [r.id for r in user.roles]
    return AUDITOR_ROLE_ID in role_ids or user.guild_permissions.administrator


# --- Bot 初始化 ---


class StampBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        await self.tree.sync()
        print("全域 Slash 指令同步完成！")


bot = StampBot()


# --- 第三階段：審核 View & 駁回原因 Modal ---


class RejectReasonModal(Modal, title="填寫駁回原因"):

    reason = TextInput(
        label="駁回原因",
        style=discord.TextStyle.paragraph,
        placeholder="請輸入不通過的原因（例如：成果截圖不完整、內容不合規定...）",
        required=True,
        max_length=300,
    )

    def __init__(
        self, audit_view: "AuditView", origin_message: discord.Message
    ):
        super().__init__()
        self.audit_view = audit_view
        self.origin_message = origin_message

    async def on_submit(self, interaction: discord.Interaction):
        if self.audit_view.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已在他處完成審核！", ephemeral=True
            )
            return

        self.audit_view.is_handled = True
        self.audit_view.stop()
        for item in self.audit_view.children:
            item.disabled = True

        # 立即回傳 ephemeral 避免 3 秒超時
        await interaction.response.send_message(
            "✅ 已駁回該任務！", ephemeral=True
        )

        target_msg = self.origin_message
        embed = (
            target_msg.embeds[0].copy()
            if target_msg and target_msg.embeds
            else None
        )
        if embed:
            embed.color = discord.Color.red()
            embed.title = "❌ 碳碳審核未通過（已駁回）"
            embed.add_field(
                name="駁回原因",
                value=self.reason.value[:1024],
                inline=False,
            )
            embed.set_footer(text=f"審核員：{interaction.user.display_name}")

        if target_msg:
            try:
                await target_msg.edit(embed=embed, view=self.audit_view)
            except discord.HTTPException:
                pass

        try:
            await interaction.channel.send(
                f"❌ 審核不通過！審核員 {interaction.user.mention} 駁回了 {self.audit_view.target_user.mention} 的任務回報。\n"
                f"**原因**：{self.reason.value}"
            )
        except discord.Forbidden:
            pass


class AuditView(View):

    def __init__(self, target_user: discord.Member, stamp_reward: float):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.stamp_reward = stamp_reward
        self.is_handled = False

    @discord.ui.button(
        label="審核通過並發放印章",
        style=discord.ButtonStyle.success,
        emoji="💮",
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not is_auditor_or_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 你沒有審核權限！", ephemeral=True
            )
            return

        if self.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已完成審核！", ephemeral=True
            )
            return

        self.is_handled = True
        self.stop()
        for item in self.children:
            item.disabled = True

        total = add_user_stamp(self.target_user.id, self.stamp_reward)

        embed = (
            interaction.message.embeds[0].copy()
            if interaction.message and interaction.message.embeds
            else None
        )
        if embed:
            embed.color = discord.Color.green()
            embed.title = "💮 碳碳審核已通過"
            embed.set_footer(
                text=f"審核員：{interaction.user.display_name} | 已發放 {self.stamp_reward:g} 枚印章"
            )

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

        try:
            await interaction.channel.send(
                f"🎉 審核通過！審核員 {interaction.user.mention} 已為 {self.target_user.mention} 增加 **{self.stamp_reward:g}** 枚印章！（目前持有：`{total:g}` 枚）"
            )
        except discord.Forbidden:
            pass

    @discord.ui.button(
        label="駁回任務", style=discord.ButtonStyle.danger, emoji="❌"
    )
    async def reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not is_auditor_or_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 你沒有審核權限！", ephemeral=True
            )
            return

        if self.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已完成審核！", ephemeral=True
            )
            return

        modal = RejectReasonModal(
            audit_view=self, origin_message=interaction.message
        )
        await interaction.response.send_modal(modal)


# --- 第二階段：回報成果 Modal 與任務進度 View ---


class TaskSubmissionModal(Modal, title="任務成果回報"):

    proof = TextInput(
        label="成果說明或連結",
        style=discord.TextStyle.paragraph,
        placeholder="請輸入完成說明、截圖圖床連結或訊息連結...",
        required=True,
        max_length=500,
    )

    def __init__(self, progress_view: "TaskProgressView"):
        super().__init__()
        self.progress_view = progress_view

    async def on_submit(self, interaction: discord.Interaction):
        if self.progress_view.is_finished:
            await interaction.response.send_message(
                "⚠️ 該任務已結案或已逾期！", ephemeral=True
            )
            return

        self.progress_view.is_finished = True
        self.progress_view.stop()
        for item in self.progress_view.children:
            item.disabled = True

        # 立即回傳確認訊息給提交者
        await interaction.response.send_message(
            "✅ 任務成果已成功提交，碳碳正在審核中！", ephemeral=True
        )

        # 更新進行中卡片狀態
        if self.progress_view.message:
            try:
                await self.progress_view.message.edit(
                    content=f"🎯 {self.progress_view.target_user.mention} 已提交任務【{self.progress_view.task_name}】！等待審核中...",
                    view=self.progress_view,
                )
            except discord.HTTPException:
                pass

        proof_text = self.proof.value.strip()

        # 恢復原汁原味的「🐾 碳碳審核中...」卡片格式
        embed = discord.Embed(
            title="🐾 碳碳審核中...",
            description=(
                f"**發布者**：{self.progress_view.author.mention}\n"
                f"**執行者**：{self.progress_view.target_user.mention}\n"
                f"**任務內容**：{self.progress_view.task_name}\n"
                f"**預計獎勵**：`{self.progress_view.stamp_reward:g}` 枚印章\n\n"
                f"**執行者回報成果**：\n{proof_text[:1024]}\n\n"
                "請審核員核對成果後點擊下方按鈕發放印章。"
            ),
            color=discord.Color.gold(),
        )

        # 擷取成果內有效圖片網址（排除 Discord 自訂表情與貼圖）
        img_urls = re.findall(
            r"https?://\S+\.(?:png|jpg|jpeg|gif|webp)(?:\?\S*)?",
            proof_text,
            re.IGNORECASE,
        )
        valid_img_url = next(
            (
                url
                for url in img_urls
                if not re.search(r"(?:cdn|media)\.discordapp\.(?:com|net)/(?:emojis|stickers)/", url)
            ),
            None,
        )

        # 若使用者有交成果圖：大圖放成果圖，右上角縮圖放碳碳
        # 若使用者沒交圖：大圖直接放碳碳 GIF
        if valid_img_url:
            embed.set_image(url=valid_img_url)
            embed.set_thumbnail(url=CARBON_CAT_GIF_URL)
        else:
            embed.set_image(url=CARBON_CAT_GIF_URL)

        audit_view = AuditView(
            target_user=self.progress_view.target_user,
            stamp_reward=self.progress_view.stamp_reward,
        )

        try:
            await interaction.channel.send(
                content=f"<@&{AUDITOR_ROLE_ID}> 有新的任務回報需要審核！",
                embed=embed,
                view=audit_view,
            )
        except discord.HTTPException:
            # 若自訂圖床報錯，回退為純碳碳圖
            embed.set_image(url=CARBON_CAT_GIF_URL)
            embed.set_thumbnail(url=None)
            await interaction.channel.send(
                content=f"<@&{AUDITOR_ROLE_ID}> 有新的任務回報需要審核！",
                embed=embed,
                view=audit_view,
            )


class TaskProgressView(View):

    def __init__(
        self,
        author: discord.Member,
        target_user: discord.Member,
        task_name: str,
        stamp_reward: float,
        time_limit: int,
    ):
        super().__init__(timeout=time_limit)
        self.author = author
        self.target_user = target_user
        self.task_name = task_name
        self.stamp_reward = stamp_reward
        self.is_finished = False
        self.message: discord.Message | None = None

    async def on_timeout(self):
        if self.is_finished:
            return

        self.is_finished = True
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=f"⌛ {self.target_user.mention} 的任務【{self.task_name}】**已超過時間**，任務自動終止！",
                    view=self,
                )
            except (discord.HTTPException, discord.Forbidden):
                pass

    @discord.ui.button(
        label="回報完成任務", style=discord.ButtonStyle.primary, emoji="✅"
    )
    async def complete_task(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.target_user.id:
            await interaction.response.send_message(
                "❌ 這不是你的任務，不能按哦！", ephemeral=True
            )
            return

        if self.is_finished:
            await interaction.response.send_message(
                "⚠️ 該任務已結案或已逾期！", ephemeral=True
            )
            return

        modal = TaskSubmissionModal(progress_view=self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="取消任務(發布者/管理員)",
        style=discord.ButtonStyle.secondary,
        emoji="🚫",
    )
    async def cancel_task(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if (
            interaction.user.id != self.author.id
            and not is_auditor_or_admin(interaction.user)
        ):
            await interaction.response.send_message(
                "❌ 只有任務發布者、審核身分組或管理員可以取消進行中的任務！",
                ephemeral=True,
            )
            return

        if self.is_finished:
            await interaction.response.send_message(
                "⚠️ 該任務已結案或已逾期！", ephemeral=True
            )
            return

        self.is_finished = True
        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"🚫 任務【{self.task_name}】已被 {interaction.user.mention} 中途取消！"
                f"（原指派對象：{self.target_user.mention}）"
            ),
            view=self,
        )


# --- 第一階段：任務發布 View ---


class TaskPublishView(View):

    def __init__(
        self,
        author: discord.Member,
        target_user: discord.Member,
        task_name: str,
        stamp_reward: float,
        time_limit: int,
    ):
        super().__init__(timeout=300)
        self.author = author
        self.target_user = target_user
        self.task_name = task_name
        self.stamp_reward = stamp_reward
        self.time_limit = time_limit
        self.is_handled = False
        self.message: discord.Message | None = None

    async def on_timeout(self):
        if self.is_handled:
            return

        self.is_handled = True
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=f"⌛ 任務【{self.task_name}】發布已超過 5 分鐘未回應，已自動關閉！",
                    view=self,
                )
            except (discord.HTTPException, discord.Forbidden):
                pass

    @discord.ui.button(
        label="接受任務", style=discord.ButtonStyle.success, emoji="⚔️"
    )
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.target_user.id:
            await interaction.response.send_message(
                "❌ 這不是指派給你的任務！", ephemeral=True
            )
            return

        if self.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已被處理或已過期！", ephemeral=True
            )
            return

        self.is_handled = True
        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"⚔️ {self.target_user.mention} 接受了由 {self.author.mention} 發布的任務【{self.task_name}】！",
            view=self,
        )

        progress_view = TaskProgressView(
            self.author,
            self.target_user,
            self.task_name,
            self.stamp_reward,
            self.time_limit,
        )
        expire_time = int(time.time()) + self.time_limit
        msg = await interaction.channel.send(
            f"⏳ {self.target_user.mention} 正在進行任務：**{self.task_name}**\n"
            f"截止時間：<t:{expire_time}:R>（<t:{expire_time}:T>），完成後請點擊下方按鈕回報。",
            view=progress_view,
        )
        progress_view.message = msg

    @discord.ui.button(
        label="拒絕任務", style=discord.ButtonStyle.danger, emoji="✖️"
    )
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.target_user.id:
            await interaction.response.send_message(
                "❌ 這不是指派給你的任務！", ephemeral=True
            )
            return

        if self.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已被處理或已過期！", ephemeral=True
            )
            return

        self.is_handled = True
        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"❌ {self.target_user.mention} 拒絕了任務【{self.task_name}】。",
            view=self,
        )

    @discord.ui.button(
        label="撤回發布(發布者/管理員)",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
    )
    async def cancel_publish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if (
            interaction.user.id != self.author.id
            and not is_auditor_or_admin(interaction.user)
        ):
            await interaction.response.send_message(
                "❌ 只有任務發布者、審核身分組或管理員可以撤回此任務！",
                ephemeral=True,
            )
            return

        if self.is_handled:
            await interaction.response.send_message(
                "⚠️ 該任務已被處理或已過期！", ephemeral=True
            )
            return

        self.is_handled = True
        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"🗑️ 任務【{self.task_name}】已由 {interaction.user.mention} 撤回發布！"
                f"（原指派對象：{self.target_user.mention}）"
            ),
            view=self,
        )


# --- 指令集 ---


@bot.tree.command(name="assign_task", description="發布一項任務給指定成員")
@app_commands.guild_only()
@app_commands.describe(
    member="要指派任務的成員",
    task="任務內容",
    stamps="完成可獲得的印章數",
    seconds="任務時限（單位：秒，預設 3600 秒 = 1 小時）",
)
async def assign_task(
    interaction: discord.Interaction,
    member: discord.Member,
    task: str,
    stamps: float,
    seconds: int = 3600,
):
    if not is_auditor_or_admin(interaction.user):
        await interaction.response.send_message(
            "❌ 只有審核身分組或管理員才能發布任務！", ephemeral=True
        )
        return

    # 防呆：不能指派任務給自己
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "❌ 不能指派任務給自己！", ephemeral=True
        )
        return

    # 防呆：不能指派給機器人
    if member.bot:
        await interaction.response.send_message(
            "❌ 不能指派任務給機器人！", ephemeral=True
        )
        return

    if stamps <= 0:
        await interaction.response.send_message(
            "❌ 獎勵印章數必須大於 0！", ephemeral=True
        )
        return

    valid_seconds = max(10, seconds)
    view = TaskPublishView(
        author=interaction.user,
        target_user=member,
        task_name=task,
        stamp_reward=round(stamps, 2),
        time_limit=valid_seconds,
    )
    await interaction.response.send_message(
        f"📜 {member.mention}，你收到來自 {interaction.user.mention} 的新任務！\n"
        f"**任務內容**：{task}\n"
        f"**獎勵印章**：`{round(stamps, 2):g}` 枚\n"
        f"**時限**：`{valid_seconds}` 秒（約 {round(valid_seconds / 60, 1)} 分鐘）",
        view=view,
    )
    try:
        view.message = await interaction.fetch_original_response()
    except Exception:
        pass


@bot.tree.command(
    name="add_stamp", description="直接為指定成員調整印章（支援正負數）"
)
@app_commands.guild_only()
@app_commands.describe(member="要調整的成員", amount="印章數量（支援小數點）")
async def add_stamp(
    interaction: discord.Interaction, member: discord.Member, amount: float
):
    if not is_auditor_or_admin(interaction.user):
        await interaction.response.send_message(
            "❌ 只有審核身分組或管理員才能直接調整印章！", ephemeral=True
        )
        return

    total = add_user_stamp(member.id, round(amount, 2))
    action = "增加" if amount >= 0 else "扣除"
    await interaction.response.send_message(
        f"💮 已為 {member.mention} {action} **{abs(round(amount, 2)):g}** 枚印章！"
        f"目前總計：**{total:g}** 枚！"
    )


@bot.tree.command(name="check_stamp", description="查看自己或指定成員的印章數量")
@app_commands.guild_only()
@app_commands.describe(member="要查詢的成員（留空則查詢自己）")
async def check_stamp(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    target = member or interaction.user
    stamps = get_user_stamp(target.id)
    await interaction.response.send_message(
        f"💮 {target.mention} 目前擁有 **{stamps:g}** 枚印章！"
    )


@bot.tree.command(name="stamp_leaderboard", description="查看伺服器印章排行榜")
@app_commands.guild_only()
async def stamp_leaderboard(interaction: discord.Interaction):
    rows = get_leaderboard(10)
    if not rows:
        await interaction.response.send_message("目前還沒有任何人獲得印章！")
        return

    embed = discord.Embed(
        title="🏆 印章排行榜 Top 10", color=discord.Color.gold()
    )
    desc = []
    for idx, (uid, count) in enumerate(rows, 1):
        desc.append(f"**第 {idx} 名**：<@{uid}> — `{count:g}` 枚")
    embed.description = "\n".join(desc)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("請先在 .env 設定 DISCORD_TOKEN！")
    bot.run(TOKEN)