import os
import subprocess
import sys
import uuid
import numpy as np
import pandas as pd
import rasterio
from flask import Flask, request, send_file, render_template, jsonify
from werkzeug.utils import secure_filename

sys.stdout.reconfigure(encoding='utf-8')   # in được tiếng Việt trên console Windows

app = Flask(__name__)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Admin-Token')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    # cho phép trang WebGIS đã đưa lên tên miền (https) gọi máy chủ này đang chạy trên máy admin (localhost)
    response.headers.add('Access-Control-Allow-Private-Network', 'true')
    return response

# Cấu hình thư mục lưu trữ tạm thời
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
# Tự động tạo thư mục nếu chưa tồn tại
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

# Mã quản trị máy chủ: lấy từ biến môi trường WEBGIS_ADMIN_TOKEN, nếu không có thì tự tạo
# và lưu vào admin_token.txt (chỉ người có quyền truy cập máy chủ mới đọc được).
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin_token.txt')

def get_admin_token():
    token = os.environ.get('WEBGIS_ADMIN_TOKEN')
    if token:
        return token
    if not os.path.exists(TOKEN_FILE):
        import secrets
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(secrets.token_urlsafe(16))
    with open(TOKEN_FILE, encoding='utf-8') as f:
        return f.read().strip()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/calculate', methods=['POST'])
def calculate_indices():
    if 'file' not in request.files:
        return jsonify({'error': 'Không tìm thấy file trong request'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Chưa chọn file'}), 400
    
    if not file.filename.endswith('.tif'):
        return jsonify({'error': 'Chỉ hỗ trợ file định dạng .tif'}), 400

    # Lưu file tạm thời
    filename = secure_filename(file.filename)
    unique_id = str(uuid.uuid4())
    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_{filename}")
    file.save(upload_path)

    try:
        # Đọc file ảnh .tif bằng rasterio
        with rasterio.open(upload_path) as src:
            # Giả định:
            # Band 1: Red
            # Band 2: NIR
            # Band 3: Thermal
            if src.count < 3:
                return jsonify({'error': 'Ảnh cần có ít nhất 3 bands (Red, NIR, Thermal)'}), 400
            
            # Chuyển dữ liệu sang float32 để tính toán
            red = src.read(1).astype('float32')
            nir = src.read(2).astype('float32')
            thermal = src.read(3).astype('float32')

        # Xử lý bỏ qua lỗi chia cho 0 hoặc NaN của numpy
        np.seterr(divide='ignore', invalid='ignore')
        
        # 1. Tính NDVI = (NIR - Red) / (NIR + Red)
        ndvi_denominator = (nir + red)
        ndvi = np.where(ndvi_denominator == 0, np.nan, (nir - red) / ndvi_denominator)
        
        # 2. Tính LST
        # (Ở đây sử dụng luôn band Thermal làm mẫu đơn giản)
        lst = thermal 
        
        # 3. Tính TVDI = (LST - LST_min) / (LST_max - LST_min)
        lst_min = np.nanmin(lst)
        lst_max = np.nanmax(lst)
        
        tvdi_denominator = lst_max - lst_min
        if tvdi_denominator == 0:
            tvdi = np.zeros_like(lst)
        else:
            tvdi = (lst - lst_min) / tvdi_denominator
            
        # 4. Thống kê các chỉ số 
        stats = []
        for name, array in [('NDVI', ndvi), ('LST', lst), ('TVDI', tvdi)]:
            valid_pixels = array[~np.isnan(array)]
            if len(valid_pixels) > 0:
                stats.append({
                    'Chỉ số': name,
                    'Min': np.nanmin(array),
                    'Max': np.nanmax(array),
                    'Mean': np.nanmean(array),
                    'Std': np.nanstd(array)
                })
            else:
                stats.append({
                    'Chỉ số': name,
                    'Min': None, 'Max': None, 'Mean': None, 'Std': None
                })
        
        # 5. Đưa kết quả vào Pandas DataFrame
        df = pd.DataFrame(stats)
        
        # 6. Xuất ra file Excel
        output_filename = f"Ket_qua_{unique_id}.xlsx"
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
        df.to_excel(output_path, index=False, engine='openpyxl')
        
        # 7. Trả file Excel về cho người dùng
        return send_file(output_path, as_attachment=True, download_name="KetQua_ChiSoVienTham.xlsx")
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    finally:
        # Xóa file ảnh upload tạm thời để giải phóng dung lượng
        if os.path.exists(upload_path):
            os.remove(upload_path)

@app.route('/rebuild-data', methods=['POST'])
def rebuild_data():
    # Chạy lại tao_du_lieu_webgis.py để cập nhật thư mục data/ của WebGIS (nút "Xử lý lại dữ liệu" của admin)
    if request.headers.get('X-Admin-Token') != get_admin_token():
        return jsonify({'error': 'Sai hoặc thiếu mã quản trị máy chủ (xem Remote_Sensing_Tool/admin_token.txt)'}), 403
    payload = request.get_json(silent=True) or {}
    input_dir = payload.get('input') or 'D:/data tiff'
    if not os.path.isdir(input_dir):
        return jsonify({'error': f'Không tìm thấy thư mục: {input_dir}'}), 400

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(project_dir, 'tao_du_lieu_webgis.py')
    proc = subprocess.run([sys.executable, script, '--input', input_dir], cwd=project_dir,
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    log = (proc.stdout + proc.stderr)[-8000:]
    if proc.returncode != 0:
        return jsonify({'error': 'Xử lý dữ liệu thất bại', 'log': log}), 500
    return jsonify({'ok': True, 'log': log})

if __name__ == '__main__':
    print(f'Mã quản trị máy chủ (nhập trong bảng Quản trị > Dữ liệu & GEE): {get_admin_token()}')
    app.run(debug=True, port=5000)
