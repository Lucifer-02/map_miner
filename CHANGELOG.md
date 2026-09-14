# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-09-14

### Added
- **Tối Ưu Hóa Băng Thông Proxy & Cache Ứng Dụng (Application-Level Route Cache & Bypass Expansion)**:
  - **Application-Level Route Cache**: Triển khai bộ nhớ đệm tầng ứng dụng [`DEFAULT_STATIC_CACHE_DIR`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) (`.cache/static_assets`) trong [`global_route_handler`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) cho các static scripts và stylesheets (`/maps/_/js/`, `/maps/_/ss/`, `/maps/res/`, và `gstatic.com` static assets). Phục vụ phản hồi tức thì với header `x-cache: HIT-ROUTE-CACHE` khi cache hit, loại bỏ hoàn toàn request tải lại qua proxy.
  - **Mở Rộng Proxy Bypass**: Mở rộng [`DEFAULT_PROXY_BYPASS`](file:///data/IMPORTANT/map_miner/src/map_miner/proxy.py) bổ sung các Google static CDN domains (`fonts.gstatic.com`, `apis.google.com`, `ssl.gstatic.com`), định tuyến trực tiếp static assets không tiêu tốn lưu lượng proxy dân cư.
  - **Mở Rộng Bộ Lọc URL Rác**: Bổ sung `"feedback-pa.clients6.google.com"`, `"ogads-pa.clients6.google.com"`, `"/maps/preview/entity"` vào `BLOCKED_URL_PATTERNS`, triệt tiêu hoàn toàn các request telemetry, quảng cáo và entity dư thừa.
- **Upper Bound Guardrails**:
  - Thiết lập hằng số [`DEFAULT_RANGE_LIMIT = 10000.0`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) (10 km ceiling radius) làm giới hạn bán kính tìm kiếm mặc định, bảo vệ hệ thống không cào lan man ra ngoài phạm vi địa lý dự kiến.
  - Đồng bộ hằng số [`DEFAULT_QUERY_TIMEOUT = 300.0`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) (5 phút) áp dụng trực tiếp xuyên suốt `scrape_google_maps`, `scrape_query_spa`, và `get_place_urls`.

### Changed
- **Chuẩn Hóa Tham Số & Hạn Chế Kiểu `None`**:
  - Bắt buộc tham số `geo_coordinates: Point` trong [`create_browser_context`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) nhằm thiết lập geolocation nhất quán, loại bỏ kiểu `Point | None = None`.
  - Chuyển đổi các tham số sang giá trị mặc định cụ thể (Upper Bound defaults) thay vì `None`:
    * `range_limit: float = DEFAULT_RANGE_LIMIT` (thay vì `float | None = None`).
    * `query_timeout: float = DEFAULT_QUERY_TIMEOUT` (thay vì `float | None = None`).
    * `stagger_delay: tuple[float, float] | float = (1.5, 3.5)` và `cache_dir: Path | str | None = DEFAULT_CACHE_DIR`.
    * Trong [`proxy.py`](file:///data/IMPORTANT/map_miner/src/map_miner/proxy.py): `password: str = ""` (thay vì `str | None = None`) cho `renew_tor_circuit_control` và `async_renew_tor_circuit_control`; `bypass: str = DEFAULT_PROXY_BYPASS` (thay vì `str | None`) cho `get_tor_rotating_proxy`.
  - Đơn giản hóa các khối kiểm tra điều kiện, loại bỏ việc rà soát `if range_limit is not None:` hay `if bypass is not None:`.
- **Hoàn Thiện Bộ Kiểm Thử**:
  - Khôi phục bộ kiểm thử dynamic versioning [`tests/test_version.py`](file:///data/IMPORTANT/map_miner/tests/test_version.py) (3 unit tests).
  - Bổ sung 6 unit tests chuyên biệt cho proxy bypass mở rộng và application-level route cache.
  - Tối ưu hóa mock trong `test_staggered_query_dispatch_spa` để loại bỏ hoàn toàn `RuntimeWarning: coroutine was never awaited`.
  - Toàn bộ test suite đạt **100% tests pass** (106 tests).
- **Đóng Gói Phân Phối (Distribution Packaging)**:
  - Đồng bộ phiên bản `0.3.1` làm Single Source of Truth tại [`src/map_miner/__init__.py`](file:///data/IMPORTANT/map_miner/src/map_miner/__init__.py).
  - Hoàn tất đóng gói package phân phối chuẩn qua Hatchling: Wheel (`dist/map_miner-0.3.1-py3-none-any.whl`) và Sdist (`dist/map_miner-0.3.1.tar.gz`).

---

## [0.3.0] - 2026-09-14

### Added
- **Anti-Bot & Stealth Fingerprinting**:
  - Chrome 131 User-Agent cùng bộ Client Hints hoàn chỉnh (`sec-ch-ua`, `sec-ch-ua-platform`, `sec-ch-ua-mobile`).
  - Giả lập WebGL context vendor/renderer (NVIDIA GeForce RTX 3060), triệt tiêu hoàn toàn dấu hiệu rò rỉ headless SwiftShader/llvmpipe.
  - Giả lập đầy đủ thuộc tính môi trường trình duyệt thật: plugins (`Chrome PDF Viewer`), languages (`en-US,en`), `hardwareConcurrency=8`, `deviceMemory=8`.
- **Staggered Query Dispatch**:
  - Thêm tham số `stagger_delay` trong [`scrape_google_maps`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) tạo độ trễ ngẫu nhiên ban đầu giữa các query, chống nghẽn và né bot detection khi nhiều tab bắn request cùng lúc trên 1 IP.
- **Đồng bộ hóa Tor Circuit Renewal**:
  - Tách module [`src/map_miner/proxy.py`](file:///data/IMPORTANT/map_miner/src/map_miner/proxy.py) với cơ chế cooldown & lock (`DEFAULT_TOR_RENEW_COOLDOWN = 15.0s`, `_tor_renew_lock`), ngăn chặn triệt để xung đột `SIGNAL NEWNYM` giữa các luồng.
- **Hỗ trợ Offline Speech-to-Text (STT)**:
  - Tích hợp thư viện `vosk` làm phương án dự phòng ngoại tuyến khi Google STT gặp sự cố mạng hoặc bị giới hạn.
  - Cơ chế Dual-stage audio fallback: lọc dải tần 300Hz-3400Hz và tự động fallback sang WAV mono 16kHz tiêu chuẩn.
- **Tùy biến SPA Preview Timeout**:
  - Bổ sung tham số `preview_timeout` và hằng số [`DEFAULT_SPA_PREVIEW_TIMEOUT = 15000`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) (15 giây), loại bỏ hoàn toàn lỗi `Timeout 5000ms exceeded while waiting for event "response"` khi sử dụng proxy có độ trễ cao.
- **Tối Ưu Chuẩn Hóa Dữ Liệu Đầu Ra (`flatten: bool = False`)**:
  - Mặc định chuẩn hóa 11 cột phẳng phổ biến nhất (`name`, `place_id`, `latitude`, `longitude`, `address`, `link`, `categories`, `rating`, `reviews_count`, `plus_code`, `city`); toàn bộ các thuộc tính phụ và thông tin chi tiết được đóng gói gọn trong cột thứ 12 `details` dưới dạng chuỗi JSON UTF-8 (`ensure_ascii=False`), tương thích hoàn hảo khi xuất Excel/CSV/Parquet mà không bị xung đột schema struct.
  - Bổ sung tham số `flatten: bool = False` vào [`scrape_google_maps`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) và hàm pure helper [`format_places_dataframe`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), cho phép truyền `flatten=True` để bung toàn bộ 28 cột phẳng nếu cần.
  - Cung cấp hằng số `DEFAULT_FLATTEN_COLUMNS` và duy trì `REQUIRED_COLUMNS` làm bí danh tương thích ngược.

### Changed
- **Nâng Timeout Giải CAPTCHA**: Tăng thời gian chờ giải câu đố reCAPTCHA từ 35.0s lên 85.0s (`DEFAULT_CAPTCHA_TIMEOUT = 85.0s`), đáp ứng tốt các thử thách âm thanh nhiều vòng (multi-round challenge).
- **Quy Chuẩn Code & Linter**: Áp dụng chuẩn kiểm thử nghiêm ngặt không nợ kỹ thuật: `uv run ruff check .`, `uv run ruff format .`, `ty check` và 90 unit tests pass 100%.

### Fixed
- Khắc phục triệt để lỗi bỏ sót địa điểm trên feed SPA do timeout 5000ms quá ngắn.
- Khắc phục lỗi đứt kết nối chéo giữa các tab khi 1 tab kích hoạt đổi mạch Tor.
- Khắc phục lỗi suy diễn schema dữ liệu trong Polars (`infer_schema_length=None`).

---

## [0.2.3] - 2026-09-08
- Chế độ cào SPA Navigation tốc độ cao kết hợp Multi-page fallback.
- Giải mã mảng dữ liệu preview JSON và thuật toán khôi phục dấu địa chỉ tiếng Việt.
- Bộ định tuyến mạng toàn cục `global_route_handler` chặn tài nguyên rác và tối ưu băng thông proxy.
