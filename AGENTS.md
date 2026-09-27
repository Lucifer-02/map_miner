# Phối hợp reviewer và worker trong map_miner

Tài liệu này quy định cách chia việc và kiểm chứng thay đổi. Bối cảnh kỹ thuật hiện hành nằm trong [CONTEXT.md](CONTEXT.md).

## Vai trò

**Reviewer** làm việc theo hướng read-only: khảo sát yêu cầu, đọc mã và tài liệu, lập Action Plan có phạm vi, các trường hợp biên và tiêu chí nghiệm thu. Reviewer giao việc cho worker, xem diff và kết quả kiểm tra, rồi kết luận `ACCEPT` hoặc `REQUEST_CHANGES`. Reviewer không trực tiếp sửa mã trong vòng triển khai đã giao.

**Worker** triển khai theo Action Plan, chỉ sửa file thuộc phạm vi đã thống nhất, bổ sung test có ý nghĩa cho thay đổi mã, tự chạy các kiểm tra bắt buộc và báo cáo kết quả. Worker không tự quyết định thay đổi kiến trúc lớn khi gặp mâu thuẫn hoặc ca biên ngoài kế hoạch.

Dùng công cụ collaboration hiện có để giao việc và trao đổi, chẳng hạn `spawn_agent`, `send_message` hoặc `followup_task` khi phù hợp. Không phụ thuộc vào cú pháp gọi agent cũ hoặc cờ kích hoạt công cụ không tồn tại.

## Quy trình

1. Reviewer đối chiếu yêu cầu với [CONTEXT.md](CONTEXT.md) và mã nguồn, rồi ghi Action Plan: file/hàm cần thay đổi, hành vi mong muốn, trường hợp biên và tiêu chí nghiệm thu.
2. Worker thực hiện, giữ nguyên thay đổi có sẵn của người dùng ngoài phạm vi nhiệm vụ. Nếu cần đổi kế hoạch, gửi Issue Report trước khi sửa nhánh có rủi ro.
3. Worker gửi tóm tắt diff, lý do kỹ thuật, kết quả kiểm tra và giới hạn còn lại.
4. Reviewer kiểm tra tính đúng đắn, kiến trúc, tác động hiệu năng/mạng, an toàn vận hành, kiểu dữ liệu và độ tin cậy của test. Nếu `REQUEST_CHANGES`, nêu việc cần sửa và lặp lại bước 2–4.

Issue Report ngắn cần có: ngữ cảnh và tác động; nguyên nhân sơ bộ cùng bằng chứng; hai phương án nếu có, kèm ưu/nhược điểm; quyết định cần reviewer đưa ra. Với bot block hoặc thay đổi Google Maps, tách rõ điều đã tái hiện và giả thuyết.

## Ranh giới kiến trúc

- [`scraper.py`](src/map_miner/scraper.py) chứa Playwright, network, điều phối SPA/fallback, timeout, retry và quản lý BrowserContext.
- [`extractor.py`](src/map_miner/extractor.py) chứa hàm xử lý dữ liệu thuần túy; không đưa I/O hoặc Playwright vào đây.
- [`proxy.py`](src/map_miner/proxy.py) quản lý proxy; [`recaptcha_solver.py`](src/map_miner/recaptcha_solver.py) xử lý CAPTCHA và diagnostics.
- Khi sửa lọc bán kính, đối chiếu mặc định 25 km ở API, helper, test và tài liệu. Cân nhắc URL thiếu tọa độ, response preview sai địa điểm, và kết quả gần xuất hiện sau các lượt cuộn xa.

## Cổng kiểm tra

Với thay đổi mã, worker chạy:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Nếu format chưa đạt, chạy `uv run ruff format .` rồi kiểm lại diff. Không định dạng đè thay đổi không liên quan của người dùng. Nếu một cổng kiểm tra thất bại, sửa lỗi hoặc báo rõ nguyên nhân và phạm vi ảnh hưởng cho reviewer.

Với thay đổi chỉ ở tài liệu, rà Markdown, liên kết và tính nhất quán với mã; chạy `git diff --check`. Không bắt buộc chạy lại Python suite khi mã không đổi.

Reviewer chỉ chấp nhận khi tiêu chí nghiệm thu đạt và các rủi ro còn lại được ghi rõ. Không xem test pass là bằng chứng Google Maps thật luôn trả đủ hoặc đúng dữ liệu.
