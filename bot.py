import os
import json
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import date

# ================= CONFIG =================

TOKEN = os.getenv("TOKEN")
APP_ID = 730          # CS2
CURRENCY = 3          # EUR
MIN_LISTINGS = 1

DATA_FILE = "data.json"

# ========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= DATA =================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"alerts": [], "daily": []}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ================= PRICE =================

async def steam_check(item, target_price, direction):
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": APP_ID,
        "currency": CURRENCY,
        "market_hash_name": item
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            data = await r.json()

            if not data.get("success"):
                return False, None, 0

            price_raw = data.get("lowest_price")
            volume = int(data.get("volume", "0").replace(",", ""))

            if not price_raw:
                return False, None, volume

            price = float(
                price_raw.replace("€", "")
                .replace(".", "")
                .replace(",", ".")
            )

            condition = price <= target_price if direction == "below" else price >= target_price
            return condition and volume >= MIN_LISTINGS, price, volume


async def csfloat_check(item, target_price, direction):
    url = "https://csfloat.com/api/v1/listings"
    params = {
        "market_hash_name": item,
        "limit": 50,
        "sort_by": "price",
        "order": "asc"
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            data = await r.json()
            listings = data.get("data", [])

            if not listings:
                return False, None, 0

            prices = [l["price"] / 100 for l in listings]
            lowest = prices[0]

            count = sum(
                1 for p in prices
                if (p <= target_price if direction == "below" else p >= target_price)
            )

            return count >= MIN_LISTINGS, lowest, count


async def should_trigger(alert):
    if alert["source"] == "steam":
        return await steam_check(alert["item"], alert["price"], alert["direction"])
    return await csfloat_check(alert["item"], alert["price"], alert["direction"])

# ================= COMMANDS =================

@tree.command(name="track", description="Track a CS2 skin price")
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam", value="steam"),
        app_commands.Choice(name="CSFloat", value="csfloat")
    ],
    direction=[
        app_commands.Choice(name="Below", value="below"),
        app_commands.Choice(name="Above", value="above")
    ]
)
async def track(interaction: discord.Interaction, item: str, source: str, direction: str, price: float):
    data = load_data()

    data["alerts"].append({
        "user": interaction.user.id,
        "channel": interaction.channel.id,
        "item": item,
        "source": source,
        "direction": direction,
        "price": price
    })

    save_data(data)

    await interaction.response.send_message(
        f"✅ Tracking **{item}** ({source}) {direction} €{price}"
    )


@tree.command(name="daily", description="Daily CS2 skin price updates")
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam", value="steam"),
        app_commands.Choice(name="CSFloat", value="csfloat")
    ],
    mode=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off")
    ]
)
async def daily(interaction: discord.Interaction, item: str, source: str, mode: str):
    data = load_data()

    if mode == "on":
        data["daily"].append({
            "user": interaction.user.id,
            "channel": interaction.channel.id,
            "item": item,
            "source": source,
            "last_sent": None
        })
        msg = "📅 Daily updates enabled"
    else:
        data["daily"] = [
            d for d in data["daily"]
            if not (d["user"] == interaction.user.id and d["item"] == item)
        ]
        msg = "❌ Daily updates disabled"

    save_data(data)
    await interaction.response.send_message(msg)


@tree.command(name="list", description="List alerts and daily subscriptions")
async def list_cmd(interaction: discord.Interaction):
    data = load_data()

    alerts = [
        f"{a['item']} ({a['source']} {a['direction']} €{a['price']})"
        for a in data["alerts"] if a["user"] == interaction.user.id
    ]

    daily = [
        f"{d['item']} ({d['source']})"
        for d in data["daily"] if d["user"] == interaction.user.id
    ]

    await interaction.response.send_message(
        "**Alerts:**\n" + ("\n".join(alerts) or "None") +
        "\n\n**Daily:**\n" + ("\n".join(daily) or "None")
    )

# ================= LOOPS =================

@tasks.loop(minutes=1)
async def alert_loop():
    data = load_data()

    for alert in data["alerts"][:]:
        triggered, price, count = await should_trigger(alert)

        if triggered:
            channel = await client.fetch_channel(alert["channel"])
            await channel.send(
                f"<@{alert['user']}> 🚨 **PRICE ALERT**\n"
                f"{alert['item']} ({alert['source']})\n"
                f"€{price} | Listings: {count}"
            )
            data["alerts"].remove(alert)

    save_data(data)


@tasks.loop(minutes=1)
async def daily_loop():
    today = date.today().isoformat()
    data = load_data()

    for d in data["daily"]:
        if d["last_sent"] == today:
            continue

        alert = {
            "item": d["item"],
            "source": d["source"],
            "direction": "below",
            "price": float("inf")
        }

        _, price, count = await should_trigger(alert)
        channel = await client.fetch_channel(d["channel"])

        await channel.send(
            f"<@{d['user']}> 📊 **Daily Price**\n"
            f"{d['item']} ({d['source']})\n"
            f"Lowest: €{price} | Listings: {count}"
        )

        d["last_sent"] = today

    save_data(data)

# ================= STARTUP =================

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    await tree.sync()
    alert_loop.start()
    daily_loop.start()

client.run(TOKEN)
