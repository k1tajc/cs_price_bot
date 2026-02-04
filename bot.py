import os
import json
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import date

# ================= CONFIG =================

TOKEN = os.getenv("TOKEN")  # set in host (Railway)
APP_ID = 730                # CS2
CURRENCY = 3                # EUR
MIN_LISTINGS = 20           # stability rule

DATA_FILE = "data.json"

# ==========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= HELPERS =================

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ================= PRICE FETCHERS =================

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

            price = float(price_raw.replace("€", "").replace(",", "."))

            condition = (
                price <= target_price if direction == "below"
                else price >= target_price
            )

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

            count = 0
            prices = []

            for l in listings:
                price = l["price"] / 100
                prices.append(price)

                if direction == "below" and price <= target_price:
                    count += 1
                elif direction == "above" and price >= target_price:
                    count += 1

            return count >= MIN_LISTINGS, prices[0], count


async def should_trigger(alert):
    if alert["source"] == "steam":
        return await steam_check(
            alert["item"],
            alert["price"],
            alert["direction"]
        )
    else:
        return await csfloat_check(
            alert["item"],
            alert["price"],
            alert["direction"]
        )

# ================= SLASH COMMANDS =================

@tree.command(name="track", description="Track a CS2 skin price")
@app_commands.describe(
    item="Market hash name (exact)",
    source="steam or csfloat",
    direction="below or above",
    price="Target price in EUR"
)
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam Market", value="steam"),
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

    arrow = "⬇️" if direction == "below" else "⬆️"

    await interaction.response.send_message(
        f"{interaction.user.mention} {arrow} Tracking **{item}**\n"
        f"Source: **{source}** | Target: **€{price}**\n"
        f"Trigger rule: **{MIN_LISTINGS}+ listings**"
    )


@tree.command(name="daily", description="Daily CS2 skin price updates")
@app_commands.choices(
    source=[
        app_commands.Choice(name="Steam Market", value="steam"),
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
        msg = "📅 Daily updates enabled."

    else:
        data["daily"] = [
            d for d in data["daily"]
            if not (d["user"] == interaction.user.id and d["item"] == item)
        ]
        msg = "❌ Daily updates disabled."

    save_data(data)
    await interaction.response.send_message(f"{interaction.user.mention} {msg}")


@tree.command(name="list", description="List your alerts and daily subscriptions")
async def list_cmd(interaction: discord.Interaction):
    data = load_data()

    alerts = [
        f"• {a['item']} ({a['source']} {a['direction']} €{a['price']})"
        for a in data["alerts"] if a["user"] == interaction.user.id
    ]

    daily = [
        f"• {d['item']} ({d['source']})"
        for d in data["daily"] if d["user"] == interaction.user.id
    ]

    msg = "**📌 Alerts:**\n" + ("\n".join(alerts) or "None")
    msg += "\n\n**📅 Daily:**\n" + ("\n".join(daily) or "None")

    await interaction.response.send_message(msg)

# ================= BACKGROUND TASKS =================

@tasks.loop(minutes=30)
async def alert_loop():
    data = load_data()

    for alert in data["alerts"][:]:
        triggered, price, count = await should_trigger(alert)

        if triggered:
            channel = client.get_channel(alert["channel"])
            if channel:
                await channel.send(
                    f"<@{alert['user']}> 🚨 **PRICE ALERT**\n"
                    f"**{alert['item']}** ({alert['source']})\n"
                    f"Price: **€{price}**\n"
                    f"Listings meeting rule: **{count}**"
                )

            data["alerts"].remove(alert)

    save_data(data)


@tasks.loop(hours=24)
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
        channel = client.get_channel(d["channel"])

        if channel and price:
            await channel.send(
                f"<@{d['user']}> 📊 **Daily Price Update**\n"
                f"**{d['item']}** ({d['source']})\n"
                f"Lowest price: **€{price}**\n"
                f"Listings checked: **{count}**"
            )

            d["last_sent"] = today

    save_data(data)

# ================= STARTUP =================

@client.event
async def on_ready():
    await tree.sync()
    alert_loop.start()
    daily_loop.start()
    print(f"Logged in as {client.user}")

client.run(TOKEN)

