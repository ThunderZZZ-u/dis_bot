import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv  # 必須有這行

# 必須在 getenv 之前載入 .env 檔案
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# 初始化 SQLite 資料庫
conn = sqlite3.connect("stamps.db")
cursor = conn.cursor()
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS user_stamps (
        user_id INTEGER PRIMARY KEY,
        stamps REAL DEFAULT 0.0
    )
"""
)
conn.commit()


class StampBot(commands.Bot):

  def __init__(self):
    intents = discord.Intents.default()
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    # 指派給指定伺服器，啟動後可立即在聊天室按 / 看到指令
    guild = discord.Object(id=1551126967754035200)
    self.tree.copy_global_to(guild=guild)
    await self.tree.sync(guild=guild)
    print("斜線指令已同步完成！")


bot = StampBot()


@bot.event
async def on_ready():
  print(f"機器人已上線：{bot.user}")


# 1. 蓋章指令（限管理員）
@bot.tree.command(name="add_stamp", description="為成員蓋章（增加印章數）")
@app_commands.describe(
    member="要蓋章的成員",
    amount="印章數量（支援小數，例如 0.5 或 1）",
    reason="任務或原因（選填）",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def add_stamp(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: float,
    reason: str = "達成任務",
):
  if amount <= 0:
    await interaction.response.send_message(
        "蓋章數量必須大於 0！", ephemeral=True
    )
    return

  cursor.execute(
      "SELECT stamps FROM user_stamps WHERE user_id = ?", (member.id,)
  )
  row = cursor.fetchone()

  if row is None:
    new_total = amount
    cursor.execute(
        "INSERT INTO user_stamps (user_id, stamps) VALUES (?, ?)",
        (member.id, new_total),
    )
  else:
    new_total = round(row[0] + amount, 2)  # 避免浮點數精度誤差
    cursor.execute(
        "UPDATE user_stamps SET stamps = ? WHERE user_id = ?",
        (new_total, member.id),
    )

  conn.commit()

  embed = discord.Embed(title="💮 蓋章成功！", color=0x2ECC71)
  embed.add_field(name="對象", value=member.mention, inline=True)
  embed.add_field(name="獲得數量", value=f"`+{amount}` 個章", inline=True)
  embed.add_field(name="目前總數", value=f"`{new_total}` 個章", inline=True)
  embed.add_field(name="原因", value=reason, inline=False)

  await interaction.response.send_message(embed=embed)


# 2. 查詢印章指令
@bot.tree.command(
    name="check_stamp", description="查詢自己或指定成員的印章數量"
)
@app_commands.describe(member="要查詢的成員（不填預設查詢自己）")
async def check_stamp(
    interaction: discord.Interaction, member: discord.Member = None
):
  target = member or interaction.user

  cursor.execute(
      "SELECT stamps FROM user_stamps WHERE user_id = ?", (target.id,)
  )
  row = cursor.fetchone()
  stamps = row[0] if row else 0.0

  embed = discord.Embed(title="📜 印章統計", color=0x3498DB)
  embed.set_thumbnail(url=target.display_avatar.url)
  embed.add_field(name="成員", value=target.mention, inline=True)
  embed.add_field(name="目前累積印章", value=f"`{stamps}` 個", inline=True)

  await interaction.response.send_message(embed=embed)


# 3. 排行榜指令
@bot.tree.command(name="stamp_leaderboard", description="查看群組印章排行榜")
async def stamp_leaderboard(interaction: discord.Interaction):
  cursor.execute(
      "SELECT user_id, stamps FROM user_stamps ORDER BY stamps DESC LIMIT 10"
  )
  rows = cursor.fetchall()

  if not rows:
    await interaction.response.send_message(
        "目前還沒有任何印章紀錄！", ephemeral=True
    )
    return

  desc = []
  medals = ["🥇", "🥈", "🥉"]
  for idx, (user_id, count) in enumerate(rows, start=1):
    rank_icon = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
    desc.append(f"{rank_icon} <@{user_id}> — **{count}** 個章")

  embed = discord.Embed(
      title="🏆 印章排行榜 Top 10",
      description="\n".join(desc),
      color=0xF1C40F,
  )
  await interaction.response.send_message(embed=embed)


# 錯誤處理
@add_stamp.error
async def on_add_stamp_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
  if isinstance(error, app_commands.MissingPermissions):
    await interaction.response.send_message(
        "❌ 權限不足：只有具備管理伺服器權限的管理員才能發放印章！",
        ephemeral=True,
    )


bot.run(TOKEN)