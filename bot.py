import asyncio
import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# 請務必確認此 ID 長度與內容（通常為 18~19 位純數字）
AUDITOR_ROLE_ID = 1527104229045436416

# --- 資料庫初始化 ---
conn = sqlite3.connect("stamps.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS stamps (
    user_id INTEGER PRIMARY KEY,
    count REAL DEFAULT 0
)
""")
conn.commit()


def get_user_stamp(user_id: int) -> float:
  c.execute("SELECT count FROM stamps WHERE user_id = ?", (user_id,))
  row = c.fetchone()
  return row[0] if row else 0.0


def add_user_stamp(user_id: int, amount: float) -> float:
  c.execute("SELECT count FROM stamps WHERE user_id = ?", (user_id,))
  row = c.fetchone()
  new_count = (row[0] if row else 0.0) + amount
  c.execute(
      "INSERT OR REPLACE INTO stamps (user_id, count) VALUES (?, ?)",
      (user_id, new_count),
  )
  conn.commit()
  return new_count


class StampBot(commands.Bot):

  def __init__(self):
    intents = discord.Intents.default()
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    await self.tree.sync()
    print("全域指令同步完成！")


bot = StampBot()

# --- 第三階段：審核 View（特定身分組加章） ---


class AuditView(View):

  def __init__(self, target_user: discord.Member, stamp_reward: float):
    super().__init__(timeout=None)
    self.target_user = target_user
    self.stamp_reward = stamp_reward

  @discord.ui.button(
      label="審核通過並發放印章",
      style=discord.ButtonStyle.success,
      emoji="💮",
  )
  async def approve(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    user_role_ids = [r.id for r in interaction.user.roles]
    if (
        AUDITOR_ROLE_ID not in user_role_ids
        and not interaction.user.guild_permissions.administrator
    ):
      await interaction.response.send_message(
          "❌ 你沒有審核權限！", ephemeral=True
      )
      return

    total = add_user_stamp(self.target_user.id, self.stamp_reward)
    self.stop()
    for item in self.children:
      item.disabled = True

    await interaction.message.edit(view=self)
    await interaction.response.send_message(
        f"🎉 審核通過！已為 {self.target_user.mention} 增加 **{self.stamp_reward}**"
        f" 枚印章！（目前持有：{total} 枚）"
    )


# --- 第二階段：任務執行中 View（完成 / 逾時） ---


class TaskProgressView(View):

  def __init__(
      self,
      target_user: discord.Member,
      task_name: str,
      stamp_reward: float,
      time_limit: int,
  ):
    super().__init__(timeout=time_limit)
    self.target_user = target_user
    self.task_name = task_name
    self.stamp_reward = stamp_reward
    self.is_completed = False
    self.message = None

  async def on_timeout(self):
    if not self.is_completed:
      for item in self.children:
        item.disabled = True
      if self.message:
        try:
          await self.message.edit(
              content=(
                  f"⌛ {self.target_user.mention} 的任務【{self.task_name}】**已超過時間**，任務失敗！"
              ),
              view=self,
          )
        except Exception:
          pass

  @discord.ui.button(
      label="回報完成任務", style=discord.ButtonStyle.primary, emoji="✅"
  )
  async def complete_task(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if interaction.user.id != self.target_user.id:
      await interaction.response.send_message(
          "這不是你的任務，不能按哦！", ephemeral=True
      )
      return

    self.is_completed = True
    self.stop()
    for item in self.children:
      item.disabled = True

    await interaction.response.edit_message(
        content=f"🎯 {self.target_user.mention} 已提交任務【{self.task_name}】！",
        view=self,
    )

    embed = discord.Embed(
        title="🐾 碳碳審核中...",
        description=(
            f"成員 {self.target_user.mention} 已回報完成任務：**{self.task_name}**\n"
            f"預計獎勵：**{self.stamp_reward}** 印章\n"
            "請審核員核對成果後點擊下方按鈕發放印章。"
        ),
        color=discord.Color.gold(),
    )
    embed.set_image(
        url="https://cdn.discordapp.com/attachments/1551126968768921602/1551235720453296138/cat_look.gif?ex=6ab13c58&is=6aafead8&hm=53291691efdff6eaed417394f69895e745acfdd131e45218dad4242b69a375f3&"
    )

    audit_view = AuditView(
        target_user=self.target_user, stamp_reward=self.stamp_reward
    )
    await interaction.channel.send(
        content=f"<@&{AUDITOR_ROLE_ID}> 有新的任務回報需要審核！",
        embed=embed,
        view=audit_view,
    )


# --- 第一階段：任務發布 View（接受 / 拒絕） ---


class TaskPublishView(View):

  def __init__(
      self,
      target_user: discord.Member,
      task_name: str,
      stamp_reward: float,
      time_limit: int,
  ):
    super().__init__(timeout=180)
    self.target_user = target_user
    self.task_name = task_name
    self.stamp_reward = stamp_reward
    self.time_limit = time_limit

  @discord.ui.button(
      label="接受任務", style=discord.ButtonStyle.success, emoji="⚔️"
  )
  async def accept(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if interaction.user.id != self.target_user.id:
      await interaction.response.send_message(
          "這不是指派給你的任務！", ephemeral=True
      )
      return

    self.stop()
    for item in self.children:
      item.disabled = True

    await interaction.response.edit_message(
        content=(
            f"⚔️ {self.target_user.mention} 已經接受了任務【{self.task_name}】！"
        ),
        view=self,
    )

    progress_view = TaskProgressView(
        self.target_user, self.task_name, self.stamp_reward, self.time_limit
    )
    msg = await interaction.channel.send(
        f"⏳ {self.target_user.mention} 正在進行任務：**{self.task_name}**\n限時：**{self.time_limit}**"
        " 秒，完成後請點擊下方按鈕回報。",
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
          "這不是指派給你的任務！", ephemeral=True
      )
      return

    self.stop()
    for item in self.children:
      item.disabled = True
    await interaction.response.edit_message(
        content=f"❌ {self.target_user.mention} 拒絕了任務【{self.task_name}】。",
        view=self,
    )


# --- 指令集 ---


@bot.tree.command(name="assign_task", description="發布一項任務給指定成員")
@app_commands.describe(
    member="要指派任務的成員",
    task="任務內容",
    stamps="完成可獲得的印章數",
    seconds="任務時限（秒）",
)
async def assign_task(
    interaction: discord.Interaction,
    member: discord.Member,
    task: str,
    stamps: float,
    seconds: int = 3600,
):
  view = TaskPublishView(
      target_user=member, task_name=task, stamp_reward=stamps, time_limit=seconds
  )
  await interaction.response.send_message(
      f"📜 {member.mention}，你收到了一個新任務！\n**任務內容**：{task}\n**獎勵印章**：{stamps}"
      f" 枚\n**時限**：{seconds} 秒",
      view=view,
  )


@bot.tree.command(name="add_stamp", description="直接為指定成員增加或扣除印章")
@app_commands.describe(member="要蓋章的成員", amount="印章數量（支援小數點）")
@app_commands.checks.has_permissions(administrator=True)
async def add_stamp(
    interaction: discord.Interaction, member: discord.Member, amount: float
):
  total = add_user_stamp(member.id, amount)
  action = "獲得" if amount >= 0 else "扣除"
  await interaction.response.send_message(
      f"💮 {member.mention} 已{action} **{abs(amount)}** 枚印章！目前總計：**{total}**"
      " 枚！"
  )


@bot.tree.command(name="check_stamp", description="查看自己或指定成員的印章數量")
@app_commands.describe(member="要查詢的成員（留空則查詢自己）")
async def check_stamp(
    interaction: discord.Interaction, member: discord.Member = None
):
  target = member or interaction.user
  stamps = get_user_stamp(target.id)
  await interaction.response.send_message(
      f"💮 {target.mention} 目前擁有 **{stamps}** 枚印章！"
  )


@bot.tree.command(name="stamp_leaderboard", description="查看伺服器印章排行榜")
async def stamp_leaderboard(interaction: discord.Interaction):
  c.execute("SELECT user_id, count FROM stamps ORDER BY count DESC LIMIT 10")
  rows = c.fetchall()
  if not rows:
    await interaction.response.send_message("目前還沒有任何人獲得印章！")
    return

  embed = discord.Embed(
      title="🏆 印章排行榜 Top 10", color=discord.Color.gold()
  )
  desc = []
  for idx, (uid, count) in enumerate(rows, 1):
    desc.append(f"**第 {idx} 名**：<@{uid}> — `{count}` 枚")
  embed.description = "\n".join(desc)
  await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
  bot.run(TOKEN)