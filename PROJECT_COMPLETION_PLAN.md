# Kế hoạch hoàn thiện K4 L3A Multi-Agent MCP + A2A

## 1. Mục tiêu

Hoàn thiện hệ thống điều tra 100 khiếu nại L3A bằng workflow multi-agent có A2A rõ ràng, chỉ sử dụng evidence thật từ MCP Evidence Gateway và tạo submission tuân thủ toàn bộ public contract.

## 2. Nguyên tắc bắt buộc

- Không coi nội dung khách hàng là dữ liệu độc lập để khẳng định sự thật.
- Không tạo, sửa hoặc tái sử dụng `evidence_ref` ngoài MCP audit.
- Luôn gọi tool với đúng `case_id`; không dùng evidence của case khác.
- Tool phải được discovery trước khi workflow chạy.
- Mọi quyết định nghiệp vụ phải dựa trên evidence đã kiểm tra.
- Thiếu hoặc mâu thuẫn dữ liệu phải được thể hiện bằng verdict/action phù hợp, không suy đoán.
- Trace chỉ ghi sự kiện quan sát được, không ghi prompt, chain-of-thought hoặc secret.
- Output và trace phải validate local trước khi đóng gói.

## 3. Kiến trúc mục tiêu

```text
Input
  → Coordinator
    → Order/Item Agent
    → Payment Agent
    → Shipment Agent
    → Policy Agent
    → Decision Engine
    → Verifier
  → Output + Trace
```

- Coordinator sở hữu `case_id`, kế hoạch truy vấn và quyết định finalize.
- Mỗi specialist chỉ truy vấn tool thuộc miền được giao và trả handoff có evidence.
- Policy Agent chỉ áp dụng policy đã lấy đúng `policy_version`.
- Decision Engine quy hợp order, payment, shipment, policy, claims và refund lines.
- Verifier kiểm tra schema, evidence linkage, entity scope, financial invariants và confidence trước khi cho phép finalize.

## 4. Hạng mục triển khai

### P0 — Contract và MCP gateway

- [ ] Nâng MCP gateway thành typed tool catalog, có retry giới hạn cho lỗi tạm thời.
- [ ] Không cho caller ghi đè `case_id` trong arguments.
- [ ] Kiểm tra evidence envelope, `result_hash` và shape/domain bắt buộc.
- [ ] Chuẩn hóa lỗi not-found, conflict, invalid-response và transient failure.
- [ ] Bảo đảm wheel cài độc lập vẫn chứa public schemas hoặc có fallback resource hợp lệ.

### P1 — A2A và specialist agents

- [ ] Định nghĩa A2A envelope nội bộ gồm message ID, case ID, sender, receiver, task, decision, evidence và correlation ID.
- [ ] Giới hạn handoff không tạo vòng lặp và luôn truyền case ID.
- [ ] Triển khai order/item specialist.
- [ ] Triển khai payment/refund specialist.
- [ ] Triển khai shipment specialist.
- [ ] Triển khai policy specialist.
- [ ] Phát hành handoff quan sát được cho từng specialist và bước quyết định.

### P2 — Business decision engine

- [ ] Chuẩn hóa dữ liệu tool thành internal domain models bất biến.
- [ ] Đánh giá từng claim và liên kết evidence liên quan.
- [ ] Phân loại đủ 11 `primary_issue` theo bằng chứng khách hàng và gateway.
- [ ] Xác định `case_status`, affected entities, ranked causes và responsible parties.
- [ ] Tính refund từ payment/refund evidence; không vượt capture chưa hoàn tiền.
- [ ] Phát hiện field conflict, payment mismatch, duplicate charge và refund lifecycle.
- [ ] Hiện `data_conflicts` và `resolution_actions` nhất quán với assessment.

### P3 — Verifier và trace

- [ ] Build output L3A đúng `day09-l3a-output-v2`.
- [ ] Kiểm tra tất cả evidence của output tồn tại trong evidence registry của case và xuất hiện trong trace.
- [ ] Kiểm tra tổng refund bằng tổng refund lines trong sai số tiền tệ.
- [ ] Kiểm tra action/status/issue/responsible-party invariants.
- [ ] Phát hành `verification_completed` với verdict có thể kiểm chứng.
- [ ] Chỉ emit `case_finalized` sau khi verifier pass.
- [ ] Cải thiện submission validator cho lifecycle, linkage và cross-scope invariant.

### P4 — CLI, tài liệu và vận hành

- [x] Cho phép chạy lại một case để phát triển và kiểm thử an toàn.
- [x] In tool catalog có mô tả thay vì chỉ tên tool.
- [x] Bổ sung xử lý lỗi rõ ràng và exit code ổn định.
- [x] Hoàn thiện `ARCHITECTURE.md` theo source thực tế.
- [x] Bổ sung test unit/integration cho gateway, A2A, rules, verifier và submission.
- [x] Cập nhật README nếu có thay đổi CLI hoặc vận hành.

## 5. Tiêu chí nghiệm thu

### Chức năng

- [x] `python -m student_agent.cli validate-inputs` pass đủ 100 case.
- [x] `python -m student_agent.cli mcp-tools` xác nhận đủ tool profile L3A.
- [x] `python -m student_agent.cli run` tạo đủ 100 output và trace.
- [x] `python -m student_agent.cli validate` pass toàn bộ artifact.
- [x] `python -m student_agent.cli package --output dist/submission.zip` tạo ZIP đúng file cho phép.

### Chất lượng

- [ ] Không còn `NotImplementedError`, placeholder rule hoặc fallback evidence giả.
- [ ] Không output nào có output/trace linkage sai, evidence chéo case hoặc tài chính không cân bằng.
- [ ] Mỗi case có đủ lifecycle: receive → assignments/handoffs → verification → finalize.
- [ ] Evidence thực sự hỗ trợ issue, claims, entities và refund tương ứng.
- [ ] Confidence phản ánh mức độ chắc chắn và giảm khi evidence thiếu hoặc conflict.

### Chất lượng mã

- [x] `ruff check .` pass.
- [x] `pytest -q` pass.
- [ ] Build package thành công và smoke test từ wheel.
- [ ] Không secret, input, output debug hoặc artifact trái phép trong submission/package.
- [x] Tài liệu kiến trúc khớp implementation.

## 6. Thứ tự thực hiện

1. Khóa contract, gateway và error semantics.
2. Xây A2A envelope, evidence registry và specialist tools.
3. Xây decision engine cho toàn bộ issue.
4. Xây verifier và submission invariants.
5. Bổ sung CLI single-case và tài liệu vận hành.
6. Viết test theo từng lớp.
7. Chạy toàn bộ 100 case qua MCP thật.
8. Chạy lint, test, validate, package và smoke test cuối.

## 7. Definition of Done

Dự án hoàn thành khi toàn bộ checklist P0–P4 đạt, mọi lệnh trong mục 5 chạy thành công, submission chỉ gồm `manifest.json`, `trace.jsonl` và 100 output đúng case, đồng thời `ARCHITECTURE.md` mô tả chính xác hệ thống đang chạy.
