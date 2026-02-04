import os
import json
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import date

TOKEN = os.getenv("TOKEN")
APP_ID = 730
CURRENCY = 3
MIN_LISTINGS = 1

DATA_FILE = "data.json"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------- DATA ----------

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"alerts": [], "daily": []}

    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ---------- PRICE ----------

async def steam_check(item, target_price, direction):
    print(f"Checking Steam price for {item}")

    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": APP_ID,
        "currency": CURRENCY,
        "market_hash_name": item
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            data = await r.json()

            print("Steam response:", data)

            if not data.get("success"):
                return False, None, 0

            price_raw = data.get("lowest_price")
            volume = int(data.get("volume", "0").replace(",", ""))

            if not price_raw:
                return False, None, volume

            price = float(price_raw.replace("€", "").replace(",", "."))

            print("Parsed price:", price)

            condition = price <= target_price if direction == "below" else price >= target_price

            print("Condition result:", condition)

            return condition and volume >= MIN_LISTINGS, price, volume


async def csfloat_check(item, target_price, direction):
    print(f"Checking CSFloat price for {item}")

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

            print("CSFloat listings found:", len(listings))

            if not listings:
                return False, None, 0

            prices = [l["price"] / 100 for l in listings]
            lowest = prices[0]

            count = sum(
                1 for p in prices
                if (p <= target_price if direction == "below" else p >= target_price)
            )

            print("Lowest:", lowest, "Count:", count)

            return count >= MIN_LISTINGS, lowest, count


async def should_trigger(alert):
    if alert["source"] == "steam":
        return await steam_check(alert["item"], alert["price"], alert["direction"])
    else:
        return await csfloat_check(alert["item"], alert["price"], alert["direction"])

# ---------- COMMANDS ----------

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

    await interaction.response.send_message("Tracking added")


# ---------- LOOPS ----------

@tasks.loop(minutes=1)
async def alert_loop():
    print("Alert loop running")

    data = load_data()

    for alert in data["alerts"][:]:
        try:
            triggered, price, count = await should_trigger(alert)

            print("Triggered:", triggered)

            if triggered:
                channel = await client.fetch_channel(alert["channel"])

                await channel.send(
                    f"<@{alert['user']}> ALERT\n"
                    f"{alert['item']} = €{price}"
                )

                data["alerts"].remove(alert)

        except Exception as e:
            print("Alert error:", e)

    save_data(data)


@alert_loop.before_loop
async def before_alert():
    await client.wait_until_ready()


# ---------- START ----------

@client.event
async def on_ready():
    print("Bot ready")

    synced = await tree.sync()
    print("Commands synced:", len(synced))

    alert_loop.start()

client.run(TOKEN)

