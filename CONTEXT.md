# Bối cảnh kỹ thuật: map_miner

`map_miner` 0.4.0 là thư viện Python 3.12+ thu thập địa điểm từ Google Maps bằng Playwright bất đồng bộ và trả về `polars.DataFrame`. Lịch sử thay đổi nằm trong [CHANGELOG.md](CHANGELOG.md).

## Điểm vào và luồng dữ liệu

Hàm công khai chính là [`scrape_google_maps`](src/map_miner/scraper.py). Đầu vào bắt buộc gồm tập từ khóa `queries`, tâm tìm kiếm `geo_coordinates: geopy.point.Point` và `zoom`. Mỗi từ khóa được xử lý độc lập. `max_places` giới hạn số địa điểm hợp lệ theo ID địa điểm nếu có, hoặc theo URL, trong mỗi từ khóa.

```python
import asyncio
from geopy.point import Point
from map_miner import scrape_google_maps


async def main():
    places = await scrape_google_maps(
        queries={"cafe"},
        geo_coordinates=Point(21.018785, 105.830415),
        zoom=16,
        max_places=3,
    )
    print(places)


asyncio.run(main())
```

Luồng chính:

1. [`scraper.py`](src/map_miner/scraper.py) mở Chromium, tạo `BrowserContext`, điều hướng trang và thu dữ liệu thô.
2. Với `use_spa=True`, scraper đọc và cuộn feed, click thẻ địa điểm và chờ response `/maps/preview/place`. Nó kiểm tra định danh và dùng DOM thẻ hoặc trang chi tiết nếu preview thiếu dữ liệu cần thiết.
3. Với `use_spa=False`, scraper đọc URL từ nhóm đầu tiên rồi tiếp tục cuộn. Nó kiểm tra dữ liệu trang chi tiết để tính số địa điểm hợp lệ. Truy vấn tìm kiếm và yêu cầu trang chi tiết có giới hạn song song riêng, đều bằng `n_semaphore`.
4. [`extractor.py`](src/map_miner/extractor.py) chỉ xử lý dữ liệu đã có trong bộ nhớ: parse preview/HTML/DOM, trích trường, tính khoảng cách và tạo DataFrame. Không đặt I/O mạng hoặc Playwright vào module này.
5. [`proxy.py`](src/map_miner/proxy.py) chuẩn hóa proxy và cấp phát theo vòng qua `ProxyRotator`. `scraper.py` phát hiện CAPTCHA ban đầu; [`recaptcha_solver.py`](src/map_miner/recaptcha_solver.py) chẩn đoán và thử giải reCAPTCHA.

[`src/map_miner/__init__.py`](src/map_miner/__init__.py) xuất API công khai. Các helper có tiền tố `_` trong scraper là nội bộ.

## Mặc định quan trọng

Các giá trị dưới đây lấy từ chữ ký `scrape_google_maps` trong [`scraper.py`](src/map_miner/scraper.py); tham số truyền vào sẽ ghi đè mặc định.

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `use_spa` | `True` | Dùng SPA; `False` chọn luồng nhiều trang. |
| `max_places` | `120` | Số địa điểm hợp lệ tối đa cho mỗi từ khóa. |
| `range_limit` | `25000.0` m | Bán kính từ `geo_coordinates`, tức 25 km. |
| `query_timeout` | `300.0` s | Ngân sách thời gian cho mỗi từ khóa. Ở luồng nhiều trang, thời gian đọc trang chi tiết tính riêng. Watchdog có thêm 10 s mặc định. |
| `place_timeout` | `45.0` s | Giới hạn xử lý một địa điểm ở luồng nhiều trang. |
| `navigation_timeout` | `30000` ms | Timeout điều hướng của Playwright. |
| `preview_timeout` | `10000` ms | Thời gian chờ preview ở SPA. |
| `captcha_timeout` | `85.0` s | Thời gian tối đa thử giải CAPTCHA. |
| `max_captcha_retries` | `2` | Số lần thử lại khi bị CAPTCHA ở luồng tìm kiếm. |
| `n_semaphore` | `8` | Giới hạn song song cho tìm kiếm và giới hạn riêng cho trang chi tiết trong luồng nhiều trang. |
| `proxy` | `None` | Không dùng proxy nếu không cấu hình. |
| `fields` | `None` | Lấy các trường trích xuất được. |
| `flatten` | `False` | Giữ cột `details` thay vì bung mọi trường. |
| `on_query_incomplete` | `None` | Hàm nhận từ khóa gốc và các lý do phát hiện truy vấn chưa hoàn tất. |

Hai helper `_scrape_query_spa` và `_get_place_urls` cũng mặc định `range_limit=25000.0`. Khi lọc trước lúc đọc chi tiết, scraper chỉ dùng tọa độ địa điểm dạng `!3d…!4d…` trong URL. Đây cũng là nguồn bổ sung khi dữ liệu của địa điểm thiếu tọa độ. Nếu URL chỉ chứa tâm viewport `@lat,lon` hoặc thiếu tọa độ, scraper tìm tọa độ của địa điểm trong dữ liệu trích xuất. Địa điểm hợp lệ có thể được giữ nếu tọa độ vẫn chưa xác định. `range_limit` áp dụng ở phía scraper.

Cờ disk cache Chromium mặc định tắt (`cache_dir=None`) để tránh tranh chấp khóa profile giữa các `BrowserContext` đồng thời; truyền `Path(...)` để chọn thư mục và `disk_cache_size` để giới hạn dung lượng. Cache tài nguyên tĩnh ở lớp ứng dụng vẫn mặc định nằm dưới `.cache/static_assets/` và được cấu hình riêng qua `static_cache_dir`. [Playwright ghi rõ routing tắt HTTP cache](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route); scraper có routing nên các cờ Chromium không bảo đảm tái sử dụng HTTP cache, còn cache static ứng dụng lưu riêng vẫn tồn tại. Trong các kiểm chứng từ Playwright 1.63.0 / Chromium 153 đến Playwright 1.65.2 / Chromium 159.0.8154.6, nhiều context dùng chung cờ `--disk-cache-dir` bị chậm response document và timeout tìm kiếm; `cache_dir=None` đảm bảo các truy vấn đồng thời điều hướng thành công mà không gặp hiện tượng sụt giảm sản lượng. Không coi đây là bằng chứng cache cũ hỏng hoặc regression upstream đã xác nhận.

Nếu fetch/body của cache tài nguyên tĩnh lỗi Playwright, scraper tiếp tục request gốc bằng trình duyệt để không bỏ route chưa xử lý; response lỗi và body chưa đọc xong không được cache. Khi fetch bị hủy, route được abort thay vì thử mạng lại. Khi đóng context, scraper dùng `unroute_all(behavior="wait")` để đợi handler đang chạy; mỗi bước đợi trước đóng, đóng context và đợi sau đóng có ngân sách 5 s (tối đa khoảng 15 s), rồi hủy task đợi riêng của context nếu vẫn treo. Hủy từ caller vẫn được truyền lại sau cleanup. Cleanup không hủy task của context khác; các timeout navigation/query và mức concurrency mặc định không đổi.

Ví dụ dùng luồng nhiều trang, bán kính 5 km và chỉ lấy các trường cần thiết:

```python
places = await scrape_google_maps(
    queries={"cafe"},
    geo_coordinates=Point(21.018785, 105.830415),
    zoom=16,
    use_spa=False,
    range_limit=5000.0,
    fields=["name", "latitude", "longitude", "link"],
    flatten=True,
)
```

`navigation_timeout` tính theo mili giây. `preview_timeout` nhận mili giây từ 1000 trở lên và giây nếu nhỏ hơn 1000. Các timeout còn lại tính theo giây. `range_limit` tính theo mét.

## Kết quả và chọn trường

Mặc định (`fields=None`, `flatten=False`), kết quả có 11 cột top-level theo [`DEFAULT_FLATTEN_COLUMNS`](src/map_miner/extractor.py): `name`, `place_id`, `latitude`, `longitude`, `address`, `link`, `categories`, `rating`, `reviews_count`, `plus_code`, `city`; thêm cột top-level `query` là chuỗi gốc trong `queries`; metadata khác được mã hóa JSON vào cột `details`. Giá trị thiếu có thể là `null`. `reviews_count` có kiểu `Int64` khi cột này tồn tại.

Khi không có kết quả ở chế độ mặc định, DataFrame rỗng vẫn giữ schema 13 cột trên. Với `flatten=True`, các trường thực sự trích được nằm ở top-level; số cột thay đổi theo dữ liệu, và khi không có kết quả cùng `fields=None`, DataFrame vẫn có cột `query` kiểu chuỗi.

`fields` chọn dữ liệu trả về. Scraper vẫn đọc tên, định danh và tọa độ cần cho xác thực, khử trùng và lọc bán kính. Kết quả luôn giữ `query` ở top-level. Với `flatten=False`, các trường trong 11 cột mặc định được giữ ở top-level, các trường yêu cầu khác nằm trong `details`. Với `flatten=True`, các trường yêu cầu có sẵn được bung thành cột. Một địa điểm xuất hiện trong nhiều truy vấn có một hàng cho mỗi truy vấn. Luồng nhiều trang có thể dùng chung dữ liệu trang chi tiết cho các truy vấn cùng thu URL.

`on_query_incomplete(query, reasons)` nhận các lỗi vận hành phát hiện được, ví dụ hết thời gian, lỗi đọc feed hoặc link đã thấy nhưng chưa đọc được dữ liệu. DataFrame vẫn chứa các hàng đã thu của truy vấn đó để caller có thể lưu và thử lại. Một truy vấn rỗng có tín hiệu Google Maps xác nhận hết hoặc không có kết quả vẫn có thể kết thúc bình thường.

## Giới hạn vận hành

- Google Maps có thể đổi DOM, payload, thứ tự kết quả hoặc chặn bot; trích xuất là best effort. Các trường và số hàng thực tế có thể thay đổi giữa các lượt chạy.
- Khi Playwright báo executable Chromium không tồn tại lúc khởi chạy, `scrape_google_maps` ném `RuntimeError` kèm hướng dẫn `uv run playwright install chromium` và giữ lỗi gốc trong `__cause__`. Áp dụng cho cả SPA và luồng nhiều trang; scraper không tự cài trình duyệt. Các lỗi khởi chạy khác vẫn có thể bị bắt và trả DataFrame rỗng; kết quả rỗng không tự chứng minh rằng Google Maps không có địa điểm.
- Cả hai luồng tiếp tục xử lý khi lượt cuộn nạp link chưa thử, kể cả sau 3 nhóm kết quả liên tiếp chỉ có địa điểm ngoài bán kính. Nếu lượt cuộn tiếp theo không nạp link mới, scraper có thể dừng. Google Maps không sắp xếp chặt theo khoảng cách, nên địa điểm chưa nạp lúc dừng vẫn có thể bị bỏ lỡ.
- Khi có định danh, scraper đối chiếu chính xác Hex ID hoặc Place ID của preview. Với URL thiếu định danh, việc ghép preview dựa vào tên trong đường dẫn nếu response cung cấp thông tin này.
- Khi CAPTCHA xuất hiện, [`RecaptchaSolver.save_captcha_diagnostics`](src/map_miner/recaptcha_solver.py) tự lưu screenshot, HTML và `meta.json` chứa URL/cookies dưới `debug/captchas/` theo mặc định. Hãy bảo vệ và dọn thư mục này theo chính sách dữ liệu của môi trường chạy.

## Thiết lập và kiểm tra

Nhóm dependency `dev` khóa Pyrefly ở bản ổn định `1.3.2`; `uv sync` cài checker này. Cấu hình `[tool.pyrefly]` trong [`pyproject.toml`](pyproject.toml) kiểm tra `src/`, `tests/` và `main.py`, với `src/` làm đường dẫn tìm import.

```bash
uv sync
uv run playwright install chromium
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
```

[`tests/`](tests) chứa kiểm thử unit và ca mô phỏng; chúng không thay thế kiểm tra Google Maps thật. Khi chỉ sửa tài liệu, rà link/nội dung và chạy `git diff --check`; khi sửa mã, chạy các kiểm tra Python ở trên. Xem [README.md](README.md) để bắt đầu nhanh và [AGENTS.md](AGENTS.md) để phối hợp reviewer/worker.
