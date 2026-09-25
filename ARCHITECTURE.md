# L3A Architecture Record

## 1. System overview

Hệ thống là workflow deterministic, không dùng LLM và không phụ thuộc OpenAI API. Mỗi case được xử lý độc lập; customer message chỉ là nội dung cần kiểm chứng, không phải ground truth.

```text
inputs/L3A_CASE_<id>.json
  → Coordinator
    → tool discovery
    → Order/Item Agent
    → Payment/Refund Agent
    → Shipment Agent
    → Policy Agent
    → deterministic diagnosis + published policy
    → Verifier
  → outputs/L3A_CASE_<id>.json
  → traces/trace.jsonl
```

Coordinator sở hữu `case_id`, quyền giao task và finalize. Coordinator không gọi MCP trực tiếp. Các specialist chỉ nhận task qua A2A bus và chỉ truy vấn tool trong allow-list riêng.

## 2. Module ownership

| Module | Trách nhiệm |
| --- | --- |
| `workflow.py` | Điều phối case, thu thập findings, compose draft và gọi verifier |
| `a2a.py` | Envelope, correlation, hop budget, loop prevention và timeout |
| `agents.py` | Tool ownership, MCP fetch, diagnosis inputs và policy application |
| `rules.py` | Chuẩn hóa evidence thành domain model và 11 primary issue |
| `evidence.py` | Evidence ledger, duplicate/scope/domain validation |
| `mcp_gateway.py` | MCP connection, typed discovery, argument/evidence validation và retry |
| `verifier.py` | Sửa output có thể thu hẹp về evidence trước finalize |
| `trace.py` | Ghi lifecycle event quan sát được, không ghi prompt hay chain-of-thought |
| `submission.py` | Validate output/trace, enforce invariants và đóng gói allow-list |

## 3. Agent and tool ownership

| Actor | Tools được phép gọi | Findings |
| --- | --- | --- |
| Coordinator | Không có | Case scope, task order, seller confirmation, draft assembly |
| Order Agent | `get_order`, `get_order_items`, `get_sellers` | Order anchor, item/seller scope |
| Payment Agent | `get_payment_timeline`, `get_order_payments`, `get_refund_timeline` | Captures, payment rows, mismatch, refund lifecycle |
| Shipment Agent | `get_shipment_summary` | Delivery timestamps, shipment mirror, late-delivery actor |
| Policy Agent | `get_policy` | Exact-version policy rule và financial decision |
| Verifier | Không có | Evidence, entity, money, policy và confidence invariants |

`get_customer_history` và `get_product_context` được discovery nhưng không được gọi khi rule hiện tại không cần chúng. Không có tool name nào được hard-code để vượt qua discovery: catalog rỗng hoặc discovery lỗi sẽ fail closed.

## 4. A2A protocol

Mỗi `A2AMessage` có `message_id`, `case_id`, `sender`, `recipient`, `intent`, payload, evidence refs, `hop` và `reply_to`.

Bus áp dụng các invariant sau:

- Chỉ actor `coordinator` được gửi task.
- Agent chỉ trả lời đúng task đã nhận và đúng `case_id`.
- Reply phải có `reply_to`, tăng hop đúng một bước và không trùng message ID.
- Mỗi `(recipient, intent)` chỉ chạy một lần để chặn loop.
- Tổng hop mặc định là 12; timeout cho một task là 420 giây.
- Task lỗi, timeout hoặc reply sai vẫn tạo handoff với intent `task_failed`, không làm mất lifecycle trace.

Các MCP call của specialist chạy tuần tự. Một case không có nhiều request MCP đồng thời.

## 5. Evidence lifecycle

1. `EvidenceGateway.discover_tools()` trả typed tool definitions.
2. Mỗi case thực hiện discovery riêng; catalog của case trước không được tái sử dụng.
3. Gateway tự chèn `case_id`, kiểm tra arguments theo input schema và không nhận override trực tiếp.
4. Evidence envelope phải hợp lệ theo `mcp-evidence-response-v1.schema.json`; `result_hash` phải có định dạng SHA-256 công khai.
5. `EvidenceLedger` kiểm tra domain, duplicate ref, nested `case_id`/`order_id` và từ chối payload rỗng.
6. Agent emit `tool_result_consumed` ngay khi evidence được admit hoặc ghi mã lỗi.
7. Output chỉ cite ref do ledger của chính case sở hữu; verifier loại mọi ref ngoài ownership.
8. Submission validator yêu cầu mọi output ref xuất hiện trong trace của cùng case.

Không tái tạo, sửa hoặc suy đoán `evidence_ref`. Không dùng evidence của case khác.

## 6. Deterministic business rules

`rules.py` chỉ dùng order row của case làm anchor. Các nguồn khác được lọc theo order ID, timestamp window, amount và status.

- Payment capture phải confirmed, dương và nằm trong 24 giờ từ order approval; capture bằng 0 không được coi là paid.
- Payment row được ghép với capture trong window; duplicate charge cần có payment identity khác nhau.
- Reconciliation mismatch phải open, phát sinh sau capture và không xuất hiện sau thời điểm mở case.
- Refund có thể cover nhiều captures; refund đã completed bị trừ khỏi outstanding balance để tránh đề xuất hoàn tiền lần hai.
- Với đơn nhiều seller, handoff trễ nếu carrier nhận sau shipping limit sớm nhất; chỉ seller có handoff trễ mới chịu trách nhiệm.
- Late delivery không có actor/shipping-limit evidence phải trở thành `insufficient_evidence`, không mặc định đổ lỗi cho logistics.
- Thiếu item, payment hoặc shipment evidence làm toàn case không đủ completeness và chuyển sang `insufficient_evidence`.
- Policy chỉ được áp dụng khi đúng `policy_version`, currency, issue, action, status, parties và refund rule.

## 7. Failure policy

| Failure | Retry | Fallback | Trace decision |
| --- | --- | --- | --- |
| Tool discovery timeout/error | Không dùng catalog cũ | Không gọi tool | Tool failure/missing evidence |
| Retryable MCP transport/timeout | Tối đa 3 lần ở gateway; read-only/idempotent | Missing evidence | Error code tương ứng |
| Not found | Không retry | Missing evidence hoặc unsupported claim theo rule | `not_found` |
| Invalid envelope/domain/scope | Không retry | Từ chối evidence | invalid/out-of-scope code |
| Source conflict | Không retry | Chọn authoritative order row hoặc loại row ngoài window | `data_conflicts` |
| Specialist/A2A failure | Không lặp loop | `task_failed`, tiếp tục finalization honest | `task_failed` |
| Verification failure | Không retry MCP | Last-resort schema-valid insufficient output | `verification_completed` |

## 8. Verification invariants

Verifier có quyền thu hẹp draft nhưng không được thêm fact mới. Nó kiểm tra:

- schema và exact `case_id`;
- evidence ownership và claim linkage;
- order/item/seller/payment/shipment entity scope;
- responsible party scope;
- tổng refund bằng tổng refund lines;
- refund không vượt captured chưa hoàn;
- issue, case status, action và policy decision nhất quán;
- confidence nằm trong `[0, 1]` và giảm khi repair hoặc thiếu evidence.

`case_finalized` chỉ được emit sau khi solver trả output đã qua verifier. Submission validator kiểm tra lại lifecycle, evidence linkage, duplicate event IDs, cross-case scope, file size và secret patterns trước khi đóng gói.

## 9. Reproducibility and operations

- Python `>=3.11`.
- Không có random seed ảnh hưởng quyết định; trace event ID dùng `secrets`.
- MCP calls tuần tự trong từng case.
- Public schemas được đóng gói trong wheel dưới `student_agent.contract_resources`.
- `run` xóa và tạo lại toàn bộ outputs/trace khi không có `--case`.
- `run --case <id>` chỉ thay output của case đó và ghi trace riêng `traces/<case_id>.jsonl`, không phá full-run trace.
- Submission chỉ chứa `manifest.json`, `trace.jsonl` và 100 output đúng case-set.
- `.env`, input, raw data, output debug và artifact ngoài submission ZIP không được track hoặc đóng gói.
