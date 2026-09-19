import json, math
from procedura_core import (Anchor, FacadeReading, build, write_obj,
                            signed_area, bearing_deg)

# --- inputs that would come from geocoder + Overpass ------------------------
ADDRESS = "1200 N St, Lincoln, NE"
anchor = Anchor(lat=40.81363, lon=-96.70430)

# L-shaped footprint, given in WGS84 as OSM would hand it over
local = [(0, 0), (28, 0), (28, 16), (12, 16), (12, 30), (0, 30)]
footprint_wgs84 = [list(anchor.to_wgs84(x, y)) for x, y in local]

# --- what the vision model returns from the photo ---------------------------
reading = FacadeReading(
    floors=4, floor_height_m=3.6, roof="flat", parapet_m=1.1,
    material="red_brick", facade_color="#8d4b3a",
    cam_lat=40.81330, cam_lon=-96.70430,   # photographer standing to the SOUTH
    cam_heading_deg=0.0,                   # looking north at the building
    confidence=0.78,
)

out = build(ADDRESS, anchor, footprint_wgs84, reading, voxel_m=1.0)
mesh, vox, man, poly = out["mesh"], out["voxels"], out["manifest"], out["poly_enu"]

write_obj(mesh, "building.obj")
with open("placement.json", "w") as f:
    json.dump(man, f, indent=2)
with open("lattice.json", "w") as f:
    json.dump({"voxel_m": vox.voxel_m, "origin_ijk": list(vox.origin_ijk),
               "size": list(vox.size), "material": vox.material,
               "runs": vox.to_runs()}, f)

# --- sanity checks ----------------------------------------------------------
print(f"footprint area      {abs(signed_area(poly)):8.1f} m2   (28x16 + 12x14 = 616)")
print(f"eave height         {reading.eave_height_m():8.2f} m    (4 x 3.6 + 1.1)")
print(f"mesh                {len(mesh.verts):4d} verts, {len(mesh.faces)} faces")
print(f"solid voxels        {vox.count():8d}        "
      f"(expect ~{abs(signed_area(poly)) * reading.eave_height_m():.0f})")
print(f"rle runs            {len(vox.to_runs()):8d}        "
      f"({len(vox.to_runs())/vox.count()*100:.1f}% of dense)")

wi = man["photo_alignment"]["wall_index"]
x0, y0 = poly[wi]; x1, y1 = poly[(wi + 1) % len(poly)]
print(f"photo -> wall {wi}, conf {man['photo_alignment']['confidence']:.2f}, "
      f"edge ({x0:.0f},{y0:.0f})->({x1:.0f},{y1:.0f})   [expect the south wall]")

# geodesy round trip
la, lo = anchor.to_wgs84(28.0, 16.0)
rx, ry = anchor.to_enu(la, lo)
print(f"ENU round trip err  {math.hypot(rx - 28, ry - 16) * 1000:8.4f} mm")

# does the manifest put the building back where it started?
print(f"manifest corner     {man['footprint_wgs84'][1][0]:.6f}, "
      f"{man['footprint_wgs84'][1][1]:.6f}")
print(f"bearing to camera   {bearing_deg(anchor.lat, anchor.lon, reading.cam_lat, reading.cam_lon):.1f} deg   [180 = due south]")