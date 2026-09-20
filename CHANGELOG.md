# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.3] - 2026-09-20

### Added
- **Cơ Chế Cứu Vãn Dữ Liệu Hai Tầng (Two-tier Zero Data Loss Rescue)**:
  - Bổ sung tham số `results_collector: list[dict[str, Any]] | None` trong [`scrape_query_spa`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) và `links_collector: set[str] | None` trong [`get_place_urls`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py).
  - Khởi tạo collector và truyền vào hàm cào trong `run_spa_query` và `run_get_urls`. Khi chạm Hard Watchdog Timeout (310s) hoặc gặp exception, scraper trả về toàn bộ dữ liệu đã tích lũy trong collector thay vì trả về rỗng (`[]` / `set()`), cứu vãn 100% số địa điểm đã cào được (trung bình 19.6 địa điểm/lần timeout).
- **Dừng Sớm Khi Liên Tiếp Ra Ngoài Bán Kính (`MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS = 3`)**:
  - Thêm hằng số `MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS = 3`. Nếu 3 lượt cuộn liên tiếp toàn bộ địa điểm mới đều vượt quá `range_limit`, scraper kích hoạt dừng cuộn sớm ngay lập tức, tiết kiệm tài nguyên khi cào các query mật độ thấp (như `courthouse`).
- **Hậu Kiểm Bán Kính Sau Bóc Tách (Post-Extraction Radius Filtering)**:
  - Đối với các thẻ địa điểm không chứa tọa độ trên URL, sau khi bóc tách `place_data` từ preview blob, hệ thống kiểm tra khoảng cách từ `latitude`/`longitude` trích xuất được. Nếu vượt quá `range_limit`, địa điểm bị loại bỏ (early drop) và không ghi nhận vào kết quả.
- **Bộ Tiện Ích Giải Phóng An Toàn (Graceful Shutdown & Safe Closure)**:
  - Bổ sung các hàm [`safe_close_page`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), [`safe_close_context`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), và [`safe_close_browser`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) hóa giải triệt để bẫy `asyncio.shield()` khi task bị huỷ (cancellation).
  - Quản lý vòng đời task trong [`global_route_handler`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) qua `_active_route_tasks`, unroute sạch sẽ trước khi đóng context, triệt tiêu hoàn toàn 479 lỗi `Task was destroyed but it is pending!`.
- **Tái Cấu Trúc Constants thành Dataclass `ScraperConfig`**:
  - Định nghĩa dataclass [`ScraperConfig`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) tập trung hóa toàn bộ các thông số timeouts, guardrails, bộ nhớ đệm, retries và concurrency delays.
  - Tích hợp tham số `config: ScraperConfig | None = None` vào [`scrape_google_maps`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), [`scrape_query_spa`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), [`get_place_urls`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) với cơ chế phân giải độ ưu tiên (precedence) linh hoạt.
  - Duy trì 100% tương thích ngược với các hằng số `DEFAULT_*` và `MAX_*` module-level từ `DEFAULT_CONFIG = ScraperConfig()`.
  - Export `ScraperConfig` và `DEFAULT_CONFIG` tại `map_miner` và `map_miner.scraper`.
- **Bóc Tách Xử Lý Dữ Liệu Thuần Túy Ra Khỏi I/O (`src/map_miner/extractor.py`)**:
  - Chuyển toàn bộ các logic xử lý dữ liệu thuần túy (pure data processing) từ [`scraper.py`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) sang [`extractor.py`](file:///data/IMPORTANT/map_miner/src/map_miner/extractor.py): `DEFAULT_FLATTEN_COLUMNS`, `REQUIRED_COLUMNS`, `make_place_url`, `extract_coordinates_from_url`, `is_preview_response_for_link`, `_get_flatten_column_type`, `format_places_dataframe`.
  - Bổ sung 2 hàm tiện ích tính khoảng cách và kiểm tra phạm vi: [`calculate_distance`](file:///data/IMPORTANT/map_miner/src/map_miner/extractor.py) và [`is_within_range`](file:///data/IMPORTANT/map_miner/src/map_miner/extractor.py) hỗ trợ cả `Point` lẫn `tuple[float, float]`.
  - Re-export toàn bộ hằng số và hàm tại [`src/map_miner/scraper.py`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) và [`src/map_miner/__init__.py`](file:///data/IMPORTANT/map_miner/src/map_miner/__init__.py), bảo đảm 100% tương thích ngược (Backward Compatibility).
  - Thay thế toàn bộ các phép tính `geodesic(...).meters` trong `scraper.py` bằng `calculate_distance(...)`.

### Changed
- **Tối Ưu Hóa Dừng Cuộn Trống**:
  - Giảm [`MAX_CONSECUTIVE_EMPTY_SCROLLS`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) từ 6 xuống 4 lượt cuộn.
- **Kiểm Tra Deadline Chủ Động & Dynamic Preview Timeout**:
  - Trong [`scrape_query_spa`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), tính toán `remaining_time` và kiểm tra deadline (`remaining_time <= 2.0s`) ngay trong vòng lặp duyệt thẻ địa điểm và trước khi cuộn feed.
  - Tự động điều chỉnh động thời gian chờ preview XHR `cur_timeout_ms` không vượt quá thời gian còn lại của query.

---

## [0.3.2] - 2026-09-19

### Fixed
- **Vòng Lặp Cuộn Vô Hạn Khi Cạn POI Hợp Lệ (Runaway Scrolling)**:
  - Sửa lỗi logic đếm `consecutive_empty_scrolls` trong [`scrape_query_spa`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py): bộ đếm giờ đây dựa trên `not found_new_in_batch` thay vì bị ràng buộc bởi `has_unprocessed`.
  - Scraper chủ động ngắt cuộn feed sau `MAX_CONSECUTIVE_EMPTY_SCROLLS` (6 lần) liên tiếp không có thêm POI hợp lệ trong bán kính, chấm dứt hoàn toàn tình trạng bị treo 300 giây (5 phút) ở các query phổ biến (`cafe`, `restaurant`, `store`, v.v.) tại khu vực thưa dân.
- **Playwright Strict Mode Violation Trong `scroll_feed`**:
  - Thêm `.first` vào `page.locator(feed_selector).first.hover()` trong [`scroll_feed`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) trước khi gọi wheel scroll, triệt tiêu 103 lỗi strict mode khi selector fallback `'div[role="main"] div[tabindex="-1"]'` khớp nhiều phần tử DOM.
- **Lỗi Driver & Pending Tasks Khi Ngắt Bằng Ctrl+C / `CancelledError`**:
  - Bọc an toàn `await asyncio.shield(resource.close())` với `except BaseException` tại tất cả các điểm giải phóng tài nguyên (`context.close()`, `browser.close()`, `page.close()`).
  - Quản lý hủy tác vụ con đồng bộ trong [`scrape_google_maps`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), loại bỏ hoàn toàn các lỗi runtime `Browser.close: Connection closed while reading from the driver` và `Task was destroyed but it is pending!`.
- **Lỗi Báo Log ERROR Sai Lệch Khi Trang 0 Kết Quả**:
  - Bổ sung hàm [`is_no_results_page`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) kiểm tra các dấu hiệu trang rỗng ("Google Maps can't find", "No results found", "Không tìm thấy kết quả", `div.Q27duf`).
  - Ghi log `INFO` thông báo không tìm thấy kết quả và trả về danh sách rỗng thay vì bắn `logger.error("Could not find results feed selector on search page.")`.

### Changed
- **Tối Ưu Hóa SPA Preview Timeout**:
  - Giảm [`DEFAULT_SPA_PREVIEW_TIMEOUT`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py) từ 15,000ms (15s) xuống 10,000ms (10s), tiết kiệm thời gian chờ lãng phí khi gặp thẻ không kích hoạt được request preview XHR trong khi vẫn đảm bảo độ trễ an toàn cho các proxy mạng chậm.
- **Hạ Mức Độ Log Early Drop**:
  - Chuyển toàn bộ các vị trí ghi log `Early drop: Place ... is ...m away` từ `INFO` xuống `DEBUG`, triệt tiêu tình trạng ngập lụt log (hơn 117,000 dòng log gây phình file 41.7MB trong log cũ).
- **Cập Nhật Tài Liệu & Docstring Thuật Toán Xếp Hạng Google Maps**:
  - Bổ sung tài liệu chính thức từ [Google Business Profile Help #7091](https://support.google.com/business/answer/7091) vào docstring của `range_limit` tại [`scrape_google_maps`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), [`scrape_query_spa`](file:///data/IMPORTANT/map_miner/src/map_miner/scraper.py), `get_place_urls` và tài liệu kiến trúc [`CONTEXT.md`](file:///data/IMPORTANT/map_miner/CONTEXT.md).
  - Làm rõ 3 yếu tố xếp hạng: Relevance, Distance, Prominence và rationale của việc áp dụng cơ chế Early Drop từng phần tử kết hợp bộ đếm cuộn rỗng an toàn.
- **Tinh Gọn Bộ Kiểm Thử (`tests/test_scraper.py`)**:
  - Cắt giảm 508 dòng code boilerplate (-18.6%), từ 2,731 dòng xuống 2,222 dòng.
  - Chuẩn hóa các mock helpers dùng chung (`MockPlaywrightContext`, `make_fake_browser`, `make_mock_page`, `make_mock_context`).
  - Tham số hóa 7 nhóm test trùng lặp qua `@pytest.mark.parametrize`, duy trì 100% độ bao phủ kiểm thử (**119/119 tests pass**).

### Added
- Export hàm [`is_no_results_page`](file:///data/IMPORTANT/map_miner/src/map_miner/__init__.py) tại tầng gốc package.
- Các hằng số `NO_RESULTS_SELECTORS` và `NO_RESULTS_TEXT_PATTERNS`.

---

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
