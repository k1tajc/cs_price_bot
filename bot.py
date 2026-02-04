import os
import json
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import date

# ================= CONFIG =================

TOKEN = os.getenv("TOKEN")
APP_ID = 730
CURRENCY = 3
MIN_LISTINGS = 1

DATA_FILE = "data.json"

# ==========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= HELPERS =================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"alerts": [], "daily": []}

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
        return await steam_check(alert["item"], alert["price"], alert["direction"])
    else:
        return await csfloat_check(alert["item"], alert["price"], alert["direction"])


# ================= COMMANDS =================

@tree.command(name="track")
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

    await interaction.response.send_message(f"Tracking {item}")


@tree.command(name="daily")
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
    else:
        data["daily"] = [
            d for d in data["daily"]
            if not (d["user"] == interaction.user.id and d["item"] == item)
        ]

    save_data(data)
    await interaction.response.send_message("Updated")


# ================= BACKGROUND TASKS =================

@tasks.loop(minutes=1)
async def alert_loop():
    print("Alert loop running")

    data = load_data()

    for alert in data["alerts"][:]:
        try:
            triggered, price, count = await should_trigger(alert)

            if triggered:
                channel = await client.fetch_channel(alert["channel"])

                await channel.send(
                    f"<@{alert['user']}> 🚨 PRICE ALERT\n"
                    f"{alert['item']} ({alert['source']})\n"
                    f"€{price} | listings: {count}"
                )

                data["alerts"].remove(alert)

        except Exception as e:
            print("Alert error:", e)

    save_data(data)


@alert_loop.before_loop
async def before_alert_loop():
    await client.wait_until_ready()


@tasks.loop(minutes=1)
async def daily_loop():
    print("Daily loop running")

    today = date.today().isoformat()
    data = load_data()

    for d in data["daily"]:
        try:
            if d["last_sent"] == today:
                continue

            alert = {
                "item": d["item"],
                "source": d["source"],
                "direction": "below",
                "price": float("inf")
            }

            _, price, count = await should_trigger(alert)

            if price:
                channel = await client.fetch_channel(d["channel"])

                await channel.send(
                    f"<@{d['user']}> 📊 Daily Update\n"
                    f"{d['item']} ({d['source']})\n"
                    f"€{price} | listings checked: {count}"
                )

                d["last_sent"] = today

        except Exception as e:
            print("Daily error:", e)

    save_data(data)


@daily_loop.before_loop
async def before_daily_loop():
    await client.wait_until_ready()


# ================= STARTUP =================

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    await tree.sync()

    alert_loop.start()
    daily_loop.start()


client.run(TOKEN)
