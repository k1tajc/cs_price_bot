import os
import json
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import date

# ================= CONFIG =================

TOKEN = os.getenv("TOKEN")
APP_ID = 730        # CS2
CURRENCY = 3        # EUR
MIN_LISTINGS = 20   # stability rule (set to 1 for testing)

DATA_FILE = "data.json"

# ==========================================

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= DATA =================

def load_data():
    if not os.path.exists(DATA_FILE):
        save_data({"alerts": [], "daily": [], "servers": {}})
    with open(DATA_FILE, "r") as f:
        d = json.load(f)
        if "servers" not in d:
            d["servers"] = {}
        return d

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_alert_channel(guild_id: int, fallback_channel_id: int) -> int:
    """Returns the configured alert channel for this server, or falls back to where the command was used."""
    data = load_data()
    server = data["servers"].get(str(guild_id), {})
    return server.get("alert_channel") or fallback_channel_id

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

# ================= PRICE FETCHERS =================

async def steam_check(item, target_price, direction):
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": APP_ID, "currency": CURRENCY, "market_hash_name": item}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)

                if not data.get("success"):
                    print(f"[Steam] No success for {item}: {data}")
                    return False, None, 0

                price_raw = data.get("lowest_price", "")
                # Fix: only strip currency symbol and spaces, then replace comma decimal
                price_raw = price_raw.replace("€", "").replace("$", "").replace(" ", "")
                # Handle both "12.34" and "12,34" formats
                if "," in price_raw and "." in price_raw:
                    price_raw = price_raw.replace(".", "").replace(",", ".")
                else:
                    price_raw = price_raw.replace(",", ".")

                volume_raw = data.get("volume", "0").replace(",", "").replace(".", "")
                volume = int(volume_raw) if volume_raw.isdigit() else 0

                if not price_raw:
                    return False, None, volume

                price = float(price_raw)
                print(f"[Steam] {item}: €{price} (volume {volume})")

                condition = price <= target_price if direction == "below" else price >= target_price
                return condition and volume >= MIN_LISTINGS, price, volume

    except Exception as e:
        print(f"[Steam] Error for {item}: {e}")
        return False, None, 0


async def csfloat_check(item, target_price, direction):
    url = "https://csfloat.com/api/v1/listings"
    params = {"market_hash_name": item, "limit": 50, "sort_by": "price", "order": "asc"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)
                listings = data.get("data", [])

                print(f"[CSFloat] {item}: {len(listings)} listings found")

                if not listings:
                    return False, None, 0

                prices = [l["price"] / 100 for l in listings]
                lowest = prices[0]
                print(f"[CSFloat] Lowest: €{lowest}")

                count = sum(
                    1 for p in prices
                    if (p <= target_price if direction == "below" else p >= target_price)
                )

                return count >= MIN_LISTINGS, lowest, count

    except Exception as e:
        print(f"[CSFloat] Error for {item}: {e}")
        return False, None, 0


async def get_price_only(item, source):
    """Used for daily updates — just fetch the lowest price, no condition."""
    if source == "steam":
        _, price, count = await steam_check(item, float("inf"), "below")
    else:
        _, price, count = await csfloat_check(item, float("inf"), "below")
    return price, count


# ================= COMMANDS =================

@tree.command(name="track", description="Track a CS2 skin price alert")
@app_commands.describe(
    item="Exact market hash name (e.g. AK-47 | Redline (Field-Tested))",
    source="Price source",
    direction="Alert direction",
    price="Target price in EUR"
)
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam Market", value="steam"),
        app_commands.Choice(name="CSFloat", value="csfloat"),
    ],
    direction=[
        app_commands.Choice(name="Below (drop alert)", value="below"),
        app_commands.Choice(name="Above (rise alert)", value="above"),
    ]
)
async def track(interaction: discord.Interaction, item: str, source: str, direction: str, price: float):
    data = load_data()
    alert_channel = get_alert_channel(interaction.guild_id, interaction.channel.id)
    data["alerts"].append({
        "user": interaction.user.id,
        "channel": alert_channel,
        "guild": interaction.guild_id,
        "item": item,
        "source": source,
        "direction": direction,
        "price": price
    })
    save_data(data)
    arrow = "⬇️" if direction == "below" else "⬆️"
    ch_mention = f"<#{alert_channel}>"
    await interaction.response.send_message(
        f"{interaction.user.mention} {arrow} Tracking **{item}**\n"
        f"Source: **{source}** | Target: **€{price}** | Min listings: **{MIN_LISTINGS}**\n"
        f"Alert will be sent to {ch_mention}"
    )


@tree.command(name="daily", description="Subscribe to daily price updates for a skin")
@app_commands.describe(item="Exact market hash name", source="Price source", mode="Enable or disable")
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam Market", value="steam"),
        app_commands.Choice(name="CSFloat", value="csfloat"),
    ],
    mode=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ]
)
async def daily(interaction: discord.Interaction, item: str, source: str, mode: str):
    data = load_data()
    if mode == "on":
        alert_channel = get_alert_channel(interaction.guild_id, interaction.channel.id)
        data["daily"].append({
            "user": interaction.user.id,
            "channel": alert_channel,
            "guild": interaction.guild_id,
            "item": item,
            "source": source,
            "last_sent": None
        })
        ch_mention = f"<#{alert_channel}>"
        msg = f"📅 Daily updates **enabled** for **{item}** ({source}) → {ch_mention}"
    else:
        data["daily"] = [
            d for d in data["daily"]
            if not (d["user"] == interaction.user.id and d["item"] == item)
        ]
        msg = f"❌ Daily updates **disabled** for **{item}**"
    save_data(data)
    await interaction.response.send_message(msg)


@tree.command(name="untrack", description="Remove a price alert")
@app_commands.describe(item="Item name to stop tracking")
async def untrack(interaction: discord.Interaction, item: str):
    data = load_data()
    before = len(data["alerts"])
    data["alerts"] = [
        a for a in data["alerts"]
        if not (a["user"] == interaction.user.id and a["item"] == item)
    ]
    save_data(data)
    removed = before - len(data["alerts"])
    await interaction.response.send_message(
        f"✅ Removed **{removed}** alert(s) for **{item}**" if removed else f"No alert found for **{item}**"
    )


@tree.command(name="list", description="List your active alerts and daily subscriptions")
async def list_cmd(interaction: discord.Interaction):
    data = load_data()
    uid = interaction.user.id

    alerts = [
        f"• **{a['item']}** ({a['source']} {a['direction']} €{a['price']})"
        for a in data["alerts"] if a["user"] == uid
    ]
    dailies = [
        f"• **{d['item']}** ({d['source']})"
        for d in data["daily"] if d["user"] == uid
    ]

    msg = "**📌 Price Alerts:**\n" + ("\n".join(alerts) or "None")
    msg += "\n\n**📅 Daily Updates:**\n" + ("\n".join(dailies) or "None")
    await interaction.response.send_message(msg)


# ================= ADMIN / SETUP COMMANDS =================

@tree.command(name="setchannel", description="[Admin] Set the channel where price alerts are sent")
@app_commands.describe(channel="The channel to send alerts to")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You need Administrator permissions to use this.", ephemeral=True)
        return

    data = load_data()
    gid = str(interaction.guild_id)
    if gid not in data["servers"]:
        data["servers"][gid] = {}
    data["servers"][gid]["alert_channel"] = channel.id
    save_data(data)

    await interaction.response.send_message(
        f"✅ Alert channel set to {channel.mention}\n"
        f"All price alerts and daily updates will be sent there."
    )


@tree.command(name="setup", description="[Admin] View current bot configuration for this server")
async def setup(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You need Administrator permissions to use this.", ephemeral=True)
        return

    data = load_data()
    gid = str(interaction.guild_id)
    server = data["servers"].get(gid, {})

    alert_ch_id = server.get("alert_channel")
    alert_ch = f"<#{alert_ch_id}>" if alert_ch_id else "Not set (uses channel where command is run)"

    total_alerts = sum(1 for a in data["alerts"] if a.get("guild") == interaction.guild_id)
    total_daily = sum(1 for d in data["daily"] if d.get("guild") == interaction.guild_id)

    await interaction.response.send_message(
        f"**⚙️ Bot Configuration**\n\n"
        f"**Alert channel:** {alert_ch}\n"
        f"**Active price alerts:** {total_alerts}\n"
        f"**Daily subscriptions:** {total_daily}\n\n"
        f"Use `/setchannel` to change where alerts are posted."
    )


@tree.command(name="clearalerts", description="[Admin] Clear all alerts for this server")
async def clearalerts(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You need Administrator permissions to use this.", ephemeral=True)
        return

    data = load_data()
    before_alerts = len(data["alerts"])
    before_daily = len(data["daily"])
    data["alerts"] = [a for a in data["alerts"] if a.get("guild") != interaction.guild_id]
    data["daily"] = [d for d in data["daily"] if d.get("guild") != interaction.guild_id]
    save_data(data)

    await interaction.response.send_message(
        f"🗑️ Cleared **{before_alerts}** alert(s) and **{before_daily}** daily subscription(s) for this server."
    )


# ================= BACKGROUND LOOPS =================

@tasks.loop(minutes=15)
async def alert_loop():
    print("[Loop] Alert loop tick")
    data = load_data()
    changed = False

    for alert in data["alerts"][:]:
        try:
            if alert["source"] == "steam":
                triggered, price, count = await steam_check(alert["item"], alert["price"], alert["direction"])
            else:
                triggered, price, count = await csfloat_check(alert["item"], alert["price"], alert["direction"])

            print(f"[Alert] {alert['item']}: triggered={triggered}, price={price}, count={count}")

            if triggered and price is not None:
                channel = await client.fetch_channel(alert["channel"])
                arrow = "⬇️" if alert["direction"] == "below" else "⬆️"
                await channel.send(
                    f"<@{alert['user']}> 🚨 **PRICE ALERT** {arrow}\n"
                    f"**{alert['item']}** ({alert['source']})\n"
                    f"Current price: **€{price:.2f}** | Listings meeting rule: **{count}**"
                )
                data["alerts"].remove(alert)
                changed = True

        except Exception as e:
            print(f"[Alert] Error processing alert for {alert['item']}: {e}")

    if changed:
        save_data(data)


@alert_loop.before_loop
async def before_alert_loop():
    await client.wait_until_ready()


@tasks.loop(hours=24)
async def daily_loop():
    print("[Loop] Daily loop tick")
    today = date.today().isoformat()
    data = load_data()
    changed = False

    for d in data["daily"]:
        try:
            if d.get("last_sent") == today:
                continue

            price, count = await get_price_only(d["item"], d["source"])

            if price is not None:
                channel = await client.fetch_channel(d["channel"])
                await channel.send(
                    f"<@{d['user']}> 📊 **Daily Price Update**\n"
                    f"**{d['item']}** ({d['source']})\n"
                    f"Lowest price: **€{price:.2f}** | Listings checked: **{count}**"
                )
                d["last_sent"] = today
                changed = True

        except Exception as e:
            print(f"[Daily] Error for {d['item']}: {e}")

    if changed:
        save_data(data)


@daily_loop.before_loop
async def before_daily_loop():
    await client.wait_until_ready()


# ================= STARTUP =================

@client.event
async def on_ready():
    print(f"[Bot] Logged in as {client.user}")

    try:
        synced = await tree.sync()
        print(f"[Bot] Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[Bot] Sync error: {e}")

    if not alert_loop.is_running():
        alert_loop.start()
        print("[Bot] Alert loop started")

    if not daily_loop.is_running():
        daily_loop.start()
        print("[Bot] Daily loop started")


client.run(TOKEN)
