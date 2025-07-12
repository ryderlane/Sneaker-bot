import os, io, re, json, asyncio, unicodedata, string
import aiohttp, pytesseract, discord
from PIL import Image
from dotenv import load_dotenv
from google.cloud import vision
from discord import app_commands
from discord.ext import commands
from scrapfly import ScrapflyClient, ScrapeConfig
# ────────────────────────────────────────────────────────────────────────────────
# ENV / DISCORD SET-UP
# ────────────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN     = os.getenv("DISCORD_TOKEN")
RAPID_KEY = os.getenv("RAPIDAPI_KEY")
SCRAPFLY_KEY = os.getenv("SCRAPFLY_KEY")
scrapfly = ScrapflyClient(key=SCRAPFLY_KEY)
intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

# ────────────────────────────────────────────────────────────────────────────────
# UTILS
# ────────────────────────────────────────────────────────────────────────────────
SKU_REGEX = re.compile(r"\b[A-Z0-9]{2,}\d{4}-\d{3}\b")   # Nike/Jordan SKU pattern

def extract_sku(img_bytes: bytes) -> str | None:
    text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)))
    match = SKU_REGEX.search(text)
    return match.group(0) if match else None


async def guess_with_vision(img_bytes: bytes) -> str | None:
    """Return a best-guess product name using Google Vision web detection."""
    client = vision.ImageAnnotatorClient()
    image  = vision.Image(content=img_bytes)
    resp   = client.web_detection(image=image).web_detection

    if resp.best_guess_labels:
        return resp.best_guess_labels[0].label
    if resp.web_entities:
        return max(resp.web_entities, key=lambda e: e.score).description
    return None


# Clean strings → slugs (used only for fallback improvements)
def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    allowed = string.ascii_letters + string.digits + " "
    cleaned = "".join(ch for ch in name if ch in allowed).lower()
    return "-".join(cleaned.split())


# ────────────────────────────────────────────────────────────────────────────────
# STOCKX HELPERS
# ────────────────────────────────────────────────────────────────────────────────
BASE_HEADERS = {
    "x-rapidapi-key": RAPID_KEY,
    "x-rapidapi-host": "stockx-scraper-api.p.rapidapi.com",
    "Content-Type":   "application/json",
}

async def price_from_stockx_slug(slug: str) -> str | None:
    url = f"https://stockx.com/{slug}"
    print("📦 Scraping product page:", url)

    try:
        result = await scrapfly.async_scrape(
            ScrapeConfig(
                url=url,
                render_js=True,
                proxy_pool="public_residential_pool",
                asp=True,
                country="US",
            )
        )
    except Exception as e:
        print("⚠️ Scrape error:", e)
        return None

    data_text = result.selector.css("script#__NEXT_DATA__::text").get()
    if not data_text:
        return None

    payload = json.loads(data_text)
    apollo = payload["props"]["pageProps"]["apolloState"]

    for value in apollo.values():
        if isinstance(value, dict) and value.get("__typename") == "Product":
            market = value.get("market", {}).get("state", {})
            lowest = market.get("lowestAsk", {}).get("amount")
            if lowest:
                return f"${lowest}"
    return None



async def price_from_stockx(product_query: str) -> str | None:
    # slugify e.g. "Air Jordan 1 Retro High" -> "air-jordan-1-retro-high"
    slug = re.sub(r"[^a-zA-Z0-9 ]", "", product_query).lower().replace(" ", "-")
    url  = f"https://stockx.com/{slug}"
    print("📡 Scraping:", url)

    # ➊ ALWAYS await async_scrape
    try:
        result = await scrapfly.async_scrape(
            ScrapeConfig(
                url=url,
                render_js=True,
                proxy_pool="public_residential_pool",
                asp=True,
                country="US",
            )
        )
    except Exception as e:
        print("⚠️ Scrapfly error:", e)
        return None

    # ➋ Grab the hidden JSON inside the Next.js script tag
    data_text = result.selector.css("script#__NEXT_DATA__::text").get()
    if not data_text:
        return None

    payload = json.loads(data_text)
    apollo  = payload["props"]["pageProps"]["apolloState"]

    # ➌ Find product block and lowestAsk
    for value in apollo.values():
        if isinstance(value, dict) and value.get("__typename") == "Product":
            market = value.get("market", {}).get("state", {})
            lowest = market.get("lowestAsk", {}).get("amount")
            if lowest:
                return f"${lowest}"

    return None



# ────────────────────────────────────────────────────────────────────────────────
# /check COMMAND
# ────────────────────────────────────────────────────────────────────────────────
@tree.command(name="check", description="Upload a sneaker photo to check StockX pricing")
@app_commands.describe(image="Upload a clear sneaker image (box label or side shot)")
async def check(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer(thinking=True)

    try:
        if not image.content_type.startswith("image/"):
            await interaction.followup.send("❌ Please upload an image file.")
            return

        print("📥 Downloading image…")
        async with aiohttp.ClientSession() as s:
            async with s.get(image.url) as r:
                img_bytes = await r.read()
        print("📦 Image downloaded")

        # 1️⃣ Try OCR for SKU on box
        product = extract_sku(img_bytes)
        if product:
            print(f"🔍 SKU detected: {product}")
        else:
            # 2️⃣ Fall back to Vision label
            print("🤖 No SKU found — using Vision")
            product = await guess_with_vision(img_bytes)
            print(f"🧠 Vision guess: {product}")

        if not product:
            await interaction.followup.send(
                "😞 Couldn't identify the sneaker. Try a clear label or stock image."
            )
            return

        # Minor query improvement: generic names → add "Retro High"
        if len(product.split()) < 3 and not re.search(r"\d", product):
            product += " Retro High"
            print("🔁 Query improved:", product)

        # 3️⃣ Query StockX
        price = await price_from_stockx(product)

        if price:
            await interaction.followup.send(f"✅ **{product}** — StockX lowest ask: {price}")
        else:
            await interaction.followup.send(
                f"🧐 Found **{product}**, but couldn't get pricing on StockX."
            )

    except Exception as exc:
        print("❌ Error:", exc)
        await interaction.followup.send(f"❌ Something went wrong:\n```{exc}```")


# ────────────────────────────────────────────────────────────────────────────────
# BOT READY
# ────────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    synced = await tree.sync()
    print(f"✅ Logged in as {bot.user} — Synced {len(synced)} command(s): {[c.name for c in synced]}")


# ────────────────────────────────────────────────────────────────────────────────
# RUN
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
