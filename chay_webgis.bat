@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data\du_lieu_phuong.json" (
    echo Chưa có dữ liệu WebGIS - đang xử lý ảnh GeoTIFF trong "D:\data tiff" ...
    python tao_du_lieu_webgis.py --input "D:/data tiff"
)
echo Khởi động máy chủ WebGIS...
echo Truy cập http://localhost:8081/webgis_v2.html để xem ứng dụng
start http://localhost:8081/webgis_v2.html
python -m http.server 8081
pause
