import requests
import geopandas as gpd
import rasterio
import numpy as np
import pandas as pd
import folium
import matplotlib.pyplot as plt
import math
from pathlib import Path
from rasterio.mask import mask as rio_mask
from rasterio.merge import merge
from matplotlib.patches import Patch

# ══════════════════════════════════════════════════════════════════════════════
# COCOA BELT DEFORESTATION PIPELINE
# Author  : Salman Alfarizi
# Sources : Hansen/UMD/Google/USGS/NASA (raster) + GADM v4.1 (vector)
# Context : EUDR Supply Chain Compliance — Indonesia Cocoa Belt
# Output  : tree_loss_sulteng.png | cocoa_deforestation_map.html
# ══════════════════════════════════════════════════════════════════════════════

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

PROVINCES = [
    "Sulawesi Tengah",
    "Sulawesi Tenggara",
    "Sulawesi Selatan",
    "Sulawesi Barat",
    "Sumatera Barat",
    "Lampung",
]

# ── STEP 1: DOWNLOAD ADMINISTRATIVE BOUNDARIES (GADM) ─────────────────────
print("=" * 60)
print("STEP 1: Download Indonesia admin boundaries (GADM)")
print("=" * 60)

gadm_path = DATA_DIR / "gadm41_IDN.gpkg"
if not gadm_path.exists():
    print("Downloading GADM...")
    r = requests.get(
        "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_IDN.gpkg",
        stream=True,
        timeout=120,
    )
    total = int(r.headers.get("content-length", 0))
    downloaded = 0
    with open(gadm_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            pct = downloaded / total * 100 if total else 0
            print(f"\r   Progress: {pct:.1f}%", end="", flush=True)
    print(f"\n✅ GADM saved to {gadm_path}")
else:
    print("✅ GADM already exists, skipping download")

gdf = gpd.read_file(gadm_path, layer="ADM_ADM_1")
print(f"   Loaded {len(gdf)} provinces")

# ── STEP 2: DOWNLOAD HANSEN RASTER TILES (AUTO) ───────────────────────────
print("\n" + "=" * 60)
print("STEP 2: Download Hansen NASA satellite tiles")
print("=" * 60)


def get_tiles(bounds):
    """
    Compute required Hansen GFC tile names from province bounding box.
    Hansen tiles are named by their top-left corner in 10x10 degree grids.
    e.g. tile covering lat -10..0 / lon 120..130 is named '00N_120E'
    """
    left, bottom, right, top = bounds
    tiles = []
    lon_start = math.floor(left / 10) * 10
    lon_end   = math.floor(right / 10) * 10
    lat_start = math.floor(bottom / 10) * 10
    lat_end   = math.floor(top / 10) * 10
    for lon in range(lon_start, lon_end + 1, 10):
        for lat in range(lat_start, lat_end + 1, 10):
            # Hansen uses top-left corner for naming → shift lat up by 10
            tile_lat = lat + 10
            lat_str = f"{abs(tile_lat):02d}{'N' if tile_lat >= 0 else 'S'}"
            lon_str = f"{abs(lon):03d}{'E' if lon >= 0 else 'W'}"
            tiles.append(f"{lat_str}_{lon_str}")
    return list(set(tiles))


def download_tile(tile):
    """Download a Hansen GFC lossyear tile if not already cached."""
    path = DATA_DIR / f"hansen_lossyear_{tile}.tif"
    if path.exists():
        print(f"   ✅ {tile} already cached, skipping")
        return
    url = (
        f"https://storage.googleapis.com/earthenginepartners-hansen/"
        f"GFC-2023-v1.11/Hansen_GFC-2023-v1.11_lossyear_{tile}.tif"
    )
    print(f"   Downloading {tile}...")
    r = requests.get(url, stream=True)
    total = int(r.headers.get("content-length", 0))
    downloaded = 0
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            pct = downloaded / total * 100 if total else 0
            print(f"\r      Progress: {pct:.1f}%", end="", flush=True)
    print(f"\n   ✅ {tile} saved")


# Compute and download all required tiles across all provinces
all_tiles = set()
for province in PROVINCES:
    prov = gdf[gdf["NAME_1"] == province]
    bounds = prov.geometry.total_bounds
    all_tiles.update(get_tiles(bounds))

print(f"   Required tiles: {sorted(all_tiles)}")
for tile in sorted(all_tiles):
    download_tile(tile)
print("✅ All tiles ready")

# ── STEP 3-4: CLIP RASTER + CALCULATE TREE LOSS ───────────────────────────
print("\n" + "=" * 60)
print("STEP 3-4: Clip Hansen tiles + Calculate tree loss per province")
print("=" * 60)


def clip_and_calculate(province):
    """
    Clip Hansen raster to province polygon and compute total tree cover loss.

    Steps:
    1. Identify required tiles from province bounds
    2. Merge tiles if province spans multiple tiles
    3. Clip merged raster to province polygon (rasterio mask)
    4. Count non-zero pixels (any year loss) and convert to hectares
       - 1 pixel = 28m x 28m = 784 m² = 0.0784 ha

    Returns dict with tree_loss_ha, risk_level, and raw pixel array.
    """
    prov = gdf[gdf["NAME_1"] == province]
    bounds = prov.geometry.total_bounds
    tiles = get_tiles(bounds)
    tile_paths = [DATA_DIR / f"hansen_lossyear_{t}.tif" for t in tiles]

    if len(tile_paths) == 1:
        with rasterio.open(tile_paths[0]) as src:
            clipped, _ = rio_mask(src, prov.geometry.values, crop=True)
    else:
        # Merge multiple tiles before clipping
        sources = [rasterio.open(p) for p in tile_paths]
        merged, transform = merge(sources)
        for s in sources:
            s.close()

        merged_path = DATA_DIR / "temp_merged.tif"
        profile = rasterio.open(tile_paths[0]).profile
        profile.update({
            "height"   : merged.shape[1],
            "width"    : merged.shape[2],
            "transform": transform,
        })
        with rasterio.open(merged_path, "w", **profile) as dst:
            dst.write(merged)
        with rasterio.open(merged_path) as src:
            clipped, _ = rio_mask(src, prov.geometry.values, crop=True)

    data = clipped[0]

    # Pixel values 1–23 represent tree loss year (2001–2023); 0 = no loss
    total_px = int((data > 0).sum())
    total_ha = round(total_px * 0.0784)

    if total_ha > 100_000:
        risk = "High"
    elif total_ha > 30_000:
        risk = "Medium"
    else:
        risk = "Low"

    return {"tree_loss_ha": total_ha, "risk_level": risk, "data": data}


results = {}
for province in PROVINCES:
    print(f"\n   Clipping {province}...")
    result = clip_and_calculate(province)
    results[province] = result
    print(f"      Tree loss : {result['tree_loss_ha']:,} ha")
    print(f"      Risk level: {result['risk_level']}")

print("\n✅ All provinces calculated!")

# ── STEP 5: BAR CHART — ANNUAL TREE LOSS (SULAWESI TENGAH) ────────────────
print("\n" + "=" * 60)
print("STEP 5: Bar chart — annual tree loss in Sulawesi Tengah")
print("=" * 60)

data_sulteng = results["Sulawesi Tengah"]["data"]
years, hectares_list = [], []
for year_code in range(1, 24):
    pixels = int((data_sulteng == year_code).sum())
    years.append(2000 + year_code)
    hectares_list.append(pixels * 0.0784)

fig, ax = plt.subplots(figsize=(14, 6))
colors = [
    "#d62728" if h > 50_000 else "#ff7f0e" if h > 20_000 else "#2ca02c"
    for h in hectares_list
]
ax.bar(years, hectares_list, color=colors, edgecolor="white", linewidth=0.5)

# Annotate peak year
max_idx = hectares_list.index(max(hectares_list))
ax.annotate(
    f"El Niño\n{hectares_list[max_idx]:,.0f} ha",
    xy=(years[max_idx], hectares_list[max_idx]),
    xytext=(years[max_idx] + 1.5, hectares_list[max_idx] - 5000),
    arrowprops=dict(arrowstyle="->", color="black"),
    fontsize=9,
)

ax.set_title(
    "Tree Cover Loss — Sulawesi Tengah (2001–2023)\n"
    "Source: Hansen/UMD/Google/USGS/NASA",
    fontsize=13,
)
ax.set_xlabel("Year")
ax.set_ylabel("Hectares")
ax.set_xticks(years)
ax.set_xticklabels(years, rotation=45)
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
ax.legend(
    handles=[
        Patch(color="#d62728", label="High   (>50,000 ha)"),
        Patch(color="#ff7f0e", label="Medium (20,000–50,000 ha)"),
        Patch(color="#2ca02c", label="Low    (<20,000 ha)"),
    ],
    loc="upper left",
)
plt.tight_layout()
plt.savefig("tree_loss_sulteng.png", dpi=150)
print("✅ Chart saved: tree_loss_sulteng.png")

# ── STEP 6: INTERACTIVE CHOROPLETH MAP ────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 6: Interactive choropleth map")
print("=" * 60)

df_results = pd.DataFrame([
    {
        "NAME_1"      : k,
        "tree_loss_ha": v["tree_loss_ha"],
        "risk_level"  : v["risk_level"],
    }
    for k, v in results.items()
])
cocoa_gdf = gdf[gdf["NAME_1"].isin(PROVINCES)].merge(df_results, on="NAME_1")

m = folium.Map(location=[-2.5, 118.0], zoom_start=5, tiles="CartoDB positron")
RISK_COLORS = {"High": "#d62728", "Medium": "#ff7f0e", "Low": "#2ca02c"}

folium.GeoJson(
    cocoa_gdf[["NAME_1", "tree_loss_ha", "risk_level", "geometry"]],
    style_function=lambda f: {
        "fillColor" : RISK_COLORS.get(f["properties"]["risk_level"], "#999"),
        "color"     : "#333",
        "weight"    : 1,
        "fillOpacity": 0.65,
    },
    highlight_function=lambda f: {
        "weight": 3, "color": "#000", "fillOpacity": 0.85,
    },
    tooltip=folium.GeoJsonTooltip(
        fields  =["NAME_1", "risk_level", "tree_loss_ha"],
        aliases =["Province", "Risk Level", "Tree Loss (ha, 2001–2023)"],
        sticky  =True,
    ),
    popup=folium.GeoJsonPopup(
        fields  =["NAME_1", "risk_level", "tree_loss_ha"],
        aliases =["Province:", "Risk Level:", "Tree Loss (ha):"],
    ),
).add_to(m)

m.get_root().html.add_child(folium.Element("""
<div style="position:fixed;bottom:30px;left:30px;z-index:1000;background:white;
     padding:14px 18px;border-radius:8px;border:1px solid #ccc;
     font-family:sans-serif;font-size:13px;">
  <b>&#127807; Deforestation Risk</b><br>
  <i style="background:#d62728;width:14px;height:14px;display:inline-block;margin-right:6px;border-radius:2px"></i>High<br>
  <i style="background:#ff7f0e;width:14px;height:14px;display:inline-block;margin-right:6px;border-radius:2px"></i>Medium<br>
  <i style="background:#2ca02c;width:14px;height:14px;display:inline-block;margin-right:6px;border-radius:2px"></i>Low<br>
  <hr style="margin:8px 0">
  <small>Source: Hansen/UMD/Google/USGS/NASA<br>
  Period: 2001&#8211;2023 | Context: EUDR Compliance</small>
</div>
"""))

m.get_root().html.add_child(folium.Element("""
<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:1000;
     background:white;padding:10px 20px;border-radius:8px;border:1px solid #ccc;
     font-family:sans-serif;font-size:15px;font-weight:600;text-align:center;">
  &#127807; Indonesia Cocoa Belt &#8212; Deforestation Risk Analysis<br>
  <span style="font-size:11px;font-weight:400;color:#555">
    EUDR Supply Chain Compliance | Data: NASA/Hansen 2001&#8211;2023
  </span>
</div>
"""))

m.save("cocoa_deforestation_map.html")
print("✅ Map saved: cocoa_deforestation_map.html")

print("\n" + "=" * 60)
print("PIPELINE COMPLETE")
print("=" * 60)
print("Outputs:")
print("  tree_loss_sulteng.png       — annual tree loss bar chart")
print("  cocoa_deforestation_map.html — interactive risk map")