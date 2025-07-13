import os, io, re, json, asyncio, unicodedata, string
from urllib.parse import quote_plus

import aiohttp, pytesseract, discord
from PIL import Image
from dotenv import load_dotenv
from google.cloud import vision
from discord import app_commands
from discord.ext import commands
from bs4 import BeautifulSoup
import brotli  # Add brotli support

# ────────────────────────────────────────────────────────────────────────────────
# ENV / DISCORD SET‑UP
# ────────────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("DISCORD_TOKEN")
RAPID_KEY    = os.getenv("RAPIDAPI_KEY")  # Now being used for sneaker database
SCRAPFLY_KEY = os.getenv("SCRAPFLY_KEY")   # no longer used but kept for future

# Add validation for required environment variables
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")

if not RAPID_KEY:
    raise ValueError("RAPIDAPI_KEY environment variable is required")

intents = discord.Intents.default()
# Only enable message_content if you actually need it for your bot
# intents.message_content = True  # This requires privileged intent approval
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

# ────────────────────────────────────────────────────────────────────────────────
# UTILS
# ────────────────────────────────────────────────────────────────────────────────
SKU_REGEX = re.compile(r"\b[A-Z0-9]{2,}\d{4}-\d{3}\b")  # Nike/Jordan SKU pattern


def extract_sku(img_bytes: bytes) -> str | None:
    try:
        text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)))
        match = SKU_REGEX.search(text)
        return match.group(0) if match else None
    except Exception as e:
        print(f"OCR Error: {e}")
        return None


authenticated_vision_client: vision.ImageAnnotatorClient | None = None


def _vision_client() -> vision.ImageAnnotatorClient:
    # Lazy‑create so cold starts don't block the first command
    global authenticated_vision_client
    if authenticated_vision_client is None:
        try:
            authenticated_vision_client = vision.ImageAnnotatorClient()
        except Exception as e:
            print(f"Vision Client Error: {e}")
            raise
    return authenticated_vision_client


async def guess_with_vision(img_bytes: bytes) -> str | None:
    """Return a best‑guess product name using Google Vision web detection."""
    try:
        client = _vision_client()
        image  = vision.Image(content=img_bytes)
        resp   = client.web_detection(image=image).web_detection

        if resp.best_guess_labels:
            return resp.best_guess_labels[0].label
        if resp.web_entities:
            return max(resp.web_entities, key=lambda e: e.score).description
        return None
    except Exception as e:
        print(f"Vision API Error: {e}")
        return None


# Clean strings → slugs (used only for fallback improvement)
def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    allowed = string.ascii_letters + string.digits + " "
    cleaned = "".join(ch for ch in name if ch in allowed).lower()
    return "-".join(cleaned.split())


# ────────────────────────────────────────────────────────────────────────────────
# RAPIDAPI SNEAKER DATABASE HELPERS
# ────────────────────────────────────────────────────────────────────────────────
RAPIDAPI_HEADERS = {
    "X-RapidAPI-Key": RAPID_KEY,
    "X-RapidAPI-Host": "the-sneaker-database.p.rapidapi.com"
}


async def _search_sneaker_database(query: str, limit: int = 20) -> list | None:
    """Search the RapidAPI Sneaker Database for products matching the query."""
    search_url = "https://the-sneaker-database.p.rapidapi.com/search"
    
    params = {
        "query": query,
        "limit": limit
    }
    
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        await asyncio.sleep(0.5)  # Rate limiting
        
        async with aiohttp.ClientSession(headers=RAPIDAPI_HEADERS, timeout=timeout) as session:
            async with session.get(search_url, params=params) as resp:
                if resp.status != 200:
                    print(f"⚠️ RapidAPI search status {resp.status}")
                    if resp.status == 429:
                        print("⚠️ Rate limit exceeded")
                    return None
                
                data = await resp.json()
                
                # The API returns results in a 'results' key
                if isinstance(data, dict) and 'results' in data:
                    return data['results']
                elif isinstance(data, list):
                    return data
                else:
                    print(f"⚠️ Unexpected response format: {type(data)}")
                    return None
                    
    except asyncio.TimeoutError:
        print("⚠️ RapidAPI search request timed out")
        return None
    except Exception as e:
        print(f"⚠️ Error in _search_sneaker_database: {e}")
        return None
    
    return None


async def _get_sneaker_details(sneaker_id: str) -> dict | None:
    """Get detailed information about a specific sneaker."""
    details_url = f"https://the-sneaker-database.p.rapidapi.com/sneakers/{sneaker_id}"
    
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        await asyncio.sleep(0.5)  # Rate limiting
        
        async with aiohttp.ClientSession(headers=RAPIDAPI_HEADERS, timeout=timeout) as session:
            async with session.get(details_url) as resp:
                if resp.status != 200:
                    print(f"⚠️ RapidAPI details status {resp.status}")
                    return None
                
                return await resp.json()
                    
    except asyncio.TimeoutError:
        print("⚠️ RapidAPI details request timed out")
        return None
    except Exception as e:
        print(f"⚠️ Error in _get_sneaker_details: {e}")
        return None
    
    return None


def _format_sneaker_info(sneaker: dict) -> tuple[str, str | None, str | None]:
    """Extract and format sneaker information from API response."""
    name = sneaker.get('name', 'Unknown Sneaker')
    brand = sneaker.get('brand', '')
    
    # Format the full name
    if brand and brand.lower() not in name.lower():
        full_name = f"{brand} {name}"
    else:
        full_name = name
    
    # Get retail price
    retail_price = sneaker.get('retailPrice')
    if retail_price:
        price_str = f"${retail_price}"
    else:
        price_str = None
    
    # Get estimated market value if available
    market_value = sneaker.get('estimatedMarketValue')
    market_str = None
    if market_value:
        market_str = f"${market_value}"
    
    return full_name, price_str, market_str


async def search_sneakers(query: str) -> tuple[str | None, str | None, str | None]:
    """High-level helper: query → product name, retail price, and market value."""
    # Clean and improve the query
    improved_query = query.replace("jordan one", "air jordan 1").replace("jordan 1", "air jordan 1")
    
    # Try improved query first
    results = await _search_sneaker_database(improved_query)
    if not results:
        # Try original query
        results = await _search_sneaker_database(query)
    
    if not results:
        # Try some common variations
        variations = [
            f"air jordan 1 {query.split()[-1] if len(query.split()) > 1 else ''}".strip(),
            f"nike {query}",
            query.replace("retro high", "").strip(),
            query.replace("retro", "").strip(),
        ]
        
        for variation in variations:
            if variation and variation != query and variation != improved_query:
                print(f"🔄 Trying variation: {variation}")
                results = await _search_sneaker_database(variation)
                if results:
                    break
    
    if not results:
        return None, None, None
    
    # Use the first (most relevant) result
    sneaker = results[0]
    full_name, retail_price, market_value = _format_sneaker_info(sneaker)
    
    return full_name, retail_price, market_value


# ────────────────────────────────────────────────────────────────────────────────
# /check COMMAND
# ────────────────────────────────────────────────────────────────────────────────
@tree.command(name="check", description="Upload a sneaker photo to check pricing from sneaker database")
@app_commands.describe(image="Upload a clear sneaker image (box label or side shot)")
async def check(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer(thinking=True)

    try:
        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.followup.send("❌ Please upload an image file.")
            return

        print("📥 Downloading image…")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(image.url) as r:
                if r.status != 200:
                    await interaction.followup.send("❌ Failed to download image.")
                    return
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
            print(f"🔁 Query improved: {product}")

        # 3️⃣ Get sneaker info from database
        product_name, retail_price, market_value = await search_sneakers(product)

        if product_name:
            response_parts = [f"✅ **{product_name}**"]
            
            if retail_price:
                response_parts.append(f"💰 Retail Price: {retail_price}")
            
            if market_value:
                response_parts.append(f"📈 Est. Market Value: {market_value}")
            
            if not retail_price and not market_value:
                response_parts.append("💭 No pricing information available")
            
            await interaction.followup.send(" — ".join(response_parts))
        else:
            await interaction.followup.send(
                f"🧐 Found **{product}**, but couldn't find it in the sneaker database."
            )

    except Exception as exc:
        print(f"❌ Error in check command: {exc}")
        await interaction.followup.send(f"❌ Something went wrong. Please try again.")


# ────────────────────────────────────────────────────────────────────────────────
# BOT READY
# ────────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"✅ Logged in as {bot.user} — Synced {len(synced)} command(s): {[c.name for c in synced]}")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")


# ────────────────────────────────────────────────────────────────────────────────
# RUN
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Bot failed to start: {e}")