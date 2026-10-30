"""
TIỀN XỬ LÝ DỮ LIỆU VIỄN THÁM CHO WEBGIS (LST - NDVI - TVDI)
============================================================
Đọc toàn bộ ảnh GeoTIFF xuất từ Google Earth Engine (LST, NDVI, TDVI/TVDI theo tháng),
tính giá trị trung bình theo từng phường/xã và xuất ra các file nhẹ để webgis_v2.html hiển thị:

    data/du_lieu_phuong.json   -> thống kê (mean, std, độ phủ) mỗi phường × mỗi tháng × mỗi chỉ số
    data/phuong_xa.geojson     -> ranh giới 168 phường/xã đã đơn giản hoá (nhẹ, load nhanh)
    data/raster/<CHI_SO>/*.png -> ảnh pixel (~120 m) mã hoá 8-bit để tô màu + tra giá trị trên web

Cách chạy (trong thư mục dự án):
    pip install rasterio numpy pillow shapely
    python tao_du_lieu_webgis.py --input "D:/data tiff"

Tên file ảnh phải có dạng  LST_HCMC_2024_01.tif / NDVI_HCMC_2021_01.tif / TDVI_HCMC_2019_08.tif
(nằm ở thư mục con nào cũng được, script tự quét đệ quy).
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import rasterize
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds, transform_geom
from shapely.geometry import mapping, shape

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Khoảng giá trị dùng để mã hoá ảnh pixel về 8-bit (0 = không có dữ liệu, 1..255 = giá trị)
ENCODE_RANGE = {
    'LST': (10.0, 70.0),    # °C  -> bước ~0.24 °C (ảnh GEE có điểm nóng tới ~70 °C)
    'NDVI': (-1.0, 1.0),    #     -> bước ~0.008
    'TVDI': (-0.4, 1.1),    #     -> bước ~0.006
}
UNITS = {'LST': '°C', 'NDVI': '', 'TVDI': ''}
FILE_RE = re.compile(r'(LST|NDVI|TDVI|TVDI)_HCMC_(\d{4})_(\d{2})\.tif$', re.IGNORECASE)
MIN_COVERAGE = 0.10   # phường có < 10% pixel hợp lệ (mây che) trong tháng -> bỏ qua tháng đó


def find_rasters(input_dir):
    found = {}
    for path in glob.glob(os.path.join(input_dir, '**', '*.tif'), recursive=True):
        m = FILE_RE.search(os.path.basename(path))
        if not m:
            continue
        idx = m.group(1).upper()
        idx = 'TVDI' if idx == 'TDVI' else idx          # GEE xuất tên TDVI -> chuẩn hoá thành TVDI
        found.setdefault(idx, {})[f'{m.group(2)}-{m.group(3)}'] = path
    return {k: dict(sorted(v.items())) for k, v in found.items()}


def load_wards(path, simplify_tol):
    with open(path, encoding='utf-8') as f:
        gj = json.load(f)
    feats = gj['features']
    simplified = []
    for i, ft in enumerate(feats):
        geom = shape(ft['geometry'])
        g2 = geom.simplify(simplify_tol, preserve_topology=True)
        gm = mapping(g2)
        gm = json.loads(json.dumps(gm), parse_float=lambda x: round(float(x), 5))
        p = ft['properties']
        simplified.append({
            'type': 'Feature',
            'id': i,
            'properties': {
                'idx': i, 'ma_xa': p.get('ma_xa'), 'ten_xa': p.get('ten_xa'), 'loai': p.get('loai'),
                'dtich_km2': p.get('dtich_km2'), 'dan_so': p.get('dan_so'), 'matdo_km2': p.get('matdo_km2'),
                'sap_nhap': p.get('sap_nhap'), 'tru_so': p.get('tru_so'),
            },
            'geometry': gm,
        })
    return feats, simplified


def ward_label_grid(feats, src):
    """Raster hoá ranh giới phường lên đúng lưới ảnh (nhãn = thứ tự phường + 1)."""
    shapes = []
    for i, ft in enumerate(feats):
        g = transform_geom('EPSG:4326', src.crs, ft['geometry'])
        shapes.append((g, i + 1))
    return rasterize(shapes, out_shape=(src.height, src.width), transform=src.transform,
                     fill=0, dtype='int16', all_touched=False)


def ward_stats(arr, valid, labels, n):
    lab = labels[valid]
    vals = arr[valid].astype('float64')
    cnt = np.bincount(lab, minlength=n + 1)
    s = np.bincount(lab, weights=vals, minlength=n + 1)
    s2 = np.bincount(lab, weights=vals * vals, minlength=n + 1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mean = s / cnt
        std = np.sqrt(np.maximum(s2 / cnt - mean * mean, 0))
    return cnt[1:], mean[1:], std[1:]


def main():
    ap = argparse.ArgumentParser(description='Tiền xử lý ảnh LST/NDVI/TVDI cho WebGIS')
    ap.add_argument('--input', default='D:/data tiff', help='Thư mục chứa ảnh GeoTIFF (quét đệ quy)')
    ap.add_argument('--wards', default=os.path.join(BASE_DIR, 'TPHCM_Phuong_Xa.geojson'))
    ap.add_argument('--out', default=os.path.join(BASE_DIR, 'data'))
    ap.add_argument('--res', type=float, default=120.0, help='Độ phân giải ảnh pixel cho web (m, EPSG:3857)')
    ap.add_argument('--simplify', type=float, default=0.00015, help='Dung sai đơn giản hoá ranh giới (độ)')
    ap.add_argument('--no-raster', action='store_true', help='Chỉ tính thống kê, không xuất ảnh PNG')
    args = ap.parse_args()

    rasters = find_rasters(args.input)
    if not rasters:
        print(f'Không tìm thấy ảnh hợp lệ trong {args.input}')
        return
    for k, v in rasters.items():
        print(f'{k}: {len(v)} ảnh ({next(iter(v))} -> {list(v)[-1]})')

    os.makedirs(args.out, exist_ok=True)
    feats, simplified = load_wards(args.wards, args.simplify)
    n = len(feats)
    with open(os.path.join(args.out, 'phuong_xa.geojson'), 'w', encoding='utf-8') as f:
        json.dump({'type': 'FeatureCollection', 'features': simplified}, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Đã ghi phuong_xa.geojson ({n} phường/xã)')

    label_cache = {}
    overlay = None
    series = {}
    ward_pixels = None

    for idx, months in rasters.items():
        series[idx] = {}
        lo, hi = ENCODE_RANGE[idx]
        if not args.no_raster:
            os.makedirs(os.path.join(args.out, 'raster', idx), exist_ok=True)
        for ym, path in months.items():
            with rasterio.open(path) as src:
                key = (src.crs.to_string(), tuple(src.transform), src.width, src.height)
                # web dùng chung một lưới ảnh pixel và một bộ đếm pixel/phường -> mọi ảnh phải cùng lưới
                if label_cache and key not in label_cache:
                    raise SystemExit(f'LỖI: {path} có lưới khác các ảnh trước (CRS/độ phân giải/phạm vi). '
                                     'Hãy xuất lại từ GEE cùng crs, scale và region với các ảnh khác.')
                if key not in label_cache:
                    label_cache[key] = ward_label_grid(feats, src)
                labels = label_cache[key]
                if ward_pixels is None:
                    ward_pixels = np.bincount(labels.ravel(), minlength=n + 1)[1:]
                arr = src.read(1)
                nod = src.nodata
                valid = np.isfinite(arr)
                if nod is not None:
                    valid &= arr != nod
                in_ward = valid & (labels > 0)
                cnt, mean, std = ward_stats(arr, in_ward, labels, n)
                cov = np.where(ward_pixels > 0, cnt / np.maximum(ward_pixels, 1), 0)
                ok = cov >= MIN_COVERAGE
                city_vals = arr[in_ward]
                series[idx][ym] = {
                    'mean': [round(float(m), 4) if o else None for m, o in zip(mean, ok)],
                    'std': [round(float(s), 4) if o else None for s, o in zip(std, ok)],
                    'cov': [round(float(c), 3) for c in cov],
                    'city': {
                        'mean': round(float(city_vals.mean()), 4) if city_vals.size else None,
                        'p05': round(float(np.percentile(city_vals, 5)), 4) if city_vals.size else None,
                        'p95': round(float(np.percentile(city_vals, 95)), 4) if city_vals.size else None,
                        'cov': round(float(city_vals.size / max(ward_pixels.sum(), 1)), 3),
                    },
                }

                if not args.no_raster:
                    dst_t, dw, dh = calculate_default_transform(src.crs, 'EPSG:3857', src.width, src.height,
                                                                *src.bounds, resolution=args.res)
                    src_arr = np.where(valid, arr, np.nan).astype('float32')
                    dst = np.full((dh, dw), np.nan, dtype='float32')
                    reproject(src_arr, dst, src_transform=src.transform, src_crs=src.crs,
                              dst_transform=dst_t, dst_crs='EPSG:3857', resampling=Resampling.average,
                              src_nodata=np.nan, dst_nodata=np.nan)
                    enc = np.zeros((dh, dw), dtype='uint8')
                    good = np.isfinite(dst)
                    enc[good] = np.clip(np.round((dst[good] - lo) / (hi - lo) * 254) + 1, 1, 255).astype('uint8')
                    Image.fromarray(enc, mode='L').save(
                        os.path.join(args.out, 'raster', idx, f'{idx}_{ym.replace("-", "_")}.png'), optimize=True)
                    if overlay is None:
                        west, south, east, north = transform_bounds('EPSG:3857', 'EPSG:4326',
                                                                    dst_t.c, dst_t.f + dst_t.e * dh,
                                                                    dst_t.c + dst_t.a * dw, dst_t.f)
                        overlay = {'width': dw, 'height': dh,
                                   'bounds': [round(west, 6), round(south, 6), round(east, 6), round(north, 6)]}
            n_ok = sum(1 for m in series[idx][ym]['mean'] if m is not None)
            print(f'  {idx} {ym}: {n_ok}/{n} phường có dữ liệu, TB toàn vùng = {series[idx][ym]["city"]["mean"]}')

    out = {
        'generated': datetime.datetime.now().isoformat(timespec='seconds'),
        'source': os.path.abspath(args.input),
        'wards': [{'ma_xa': f['properties'].get('ma_xa'), 'ten_xa': f['properties'].get('ten_xa'),
                   'loai': f['properties'].get('loai'), 'pixels': int(p)}
                  for f, p in zip(feats, ward_pixels)],
        'indices': {
            k: {'months': list(series[k].keys()), 'unit': UNITS[k], 'encode': list(ENCODE_RANGE[k]),
                'files': {ym: f'raster/{k}/{k}_{ym.replace("-", "_")}.png' for ym in series[k]} if not args.no_raster else {}}
            for k in series
        },
        'overlay': overlay,
        'series': series,
    }
    with open(os.path.join(args.out, 'du_lieu_phuong.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(f'\nXong! Đã ghi {os.path.join(args.out, "du_lieu_phuong.json")}')


if __name__ == '__main__':
    main()
