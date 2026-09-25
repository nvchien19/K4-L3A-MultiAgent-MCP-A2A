# L3A Architecture Record

Tài liệu mô tả thiết kế đang chạy trong `src/student_agent/`. Mọi quyết định bên dưới kiểm chứng được qua source, `tests/test_workflow.py` và `traces/trace.jsonl`. Workflow không dùng LLM nên không có prompt hay chain-of-thought.

## 1. System overview

`day09 run` mở **một** MCP session (Streamable HTTP, Bearer team key), gọi `list_tools()` rồi xử lý các case **tuần tự**. Với mỗi case, CLI ghi `case_received`, gọi `solve_case`, validate output theo `l3a-output-v2`, ghi `outputs/<case_id>.json` rồi ghi `case_finalized`.

Trong `solve_case` (`workflow.py`), coordinator tạo `CaseContext`, `EvidenceLedger` và `A2ABus` riêng cho case rồi điều phối:

```text
inputs/<case_id>.json
      │  claimed_order_id chỉ là khóa tra cứu, không phải sự thật
      ▼
Coordinator ──(case_received)
      │  A2A bus riêng cho case: task_assigned ─► handoff
      ├─1─► order-agent ───── get_order ‖ get_order_items
      │                         └─► OrderFacts (anchor) + ItemView
      ├─2─► payment-agent ─── get_payment_timeline ‖ get_order_payments ‖ get_refund_timeline
      │     shipment-agent ── get_shipment_summary         (hai nhánh chạy song song)
      ├─3─► policy-agent ──── rules.diagnose + get_policy ─► policy_decided
      ├─4─► order-agent ───── get_sellers    (chỉ khi policy quy trách nhiệm cho seller)
      ├─5── soạn draft ────── chỉ từ EvidenceLedger + PolicyDecision
      └─6─► verifier ──────── 8 invariant ─► verification_completed
      ▼
outputs/<case_id>.json ──(case_finalized)

Mọi MCP call mang case_id của case đang chạy; mọi bước ghi vào traces/trace.jsonl.
```

Specialist không nói chuyện trực tiếp với nhau; coordinator là trung tâm. Nếu bước 1 không tìm thấy order row, bước 2 và 4 bị bỏ qua và case kết thúc ở `insufficient_evidence`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator (`workflow.py`) | Case input: `case_id`, `claimed_order_id`, `claims`, `opened_at`, `policy_version` | Không gọi MCP. Mở ledger theo case, giao task qua bus, gom findings, soạn draft chỉ từ evidence đã admit, gửi verifier | Task cho specialist; draft → verifier; output đã verify → CLI |
| Order/item (`order-agent`) | `collect_order_context`; `confirm_responsible_seller` + danh sách seller ứng viên | Lấy order row authoritative (anchor cho mọi rule) và các item row có shipping limit nằm trong cửa sổ của đơn; xác nhận seller qua registry | `order_context_ready` {OrderFacts, ItemView}; `seller_confirmed` / `seller_unconfirmed` |
| Payment (`payment-agent`) | `collect_payment_context` + OrderFacts | Chọn capture `confirmed` trong 24 h sau approval và trước `opened_at`; mismatch trong 24 h sau một capture; payment row khớp số tiền capture; refund event sau thanh toán và trước `opened_at` | `payment_context_ready` {PaymentView, RefundView, timeline_available} |
| Shipment (`shipment-agent`) | `collect_shipment_context` + OrderFacts + ItemView | Xác định trễ giao theo ngày của order row; so handoff với shipping limit; event `delivered_late` phải khớp đúng ngày giao; ghi các field lệch với order row | `shipment_context_ready` {ShipmentView} |
| Policy (`policy-agent`) | `decide_policy` + findings + cờ `complete` | Chẩn đoán issue theo thứ tự ưu tiên cố định (bên dưới), áp rule của đúng `policy_version`, refund = min(mức policy, số tiền có evidence) | `policy_decided` {PolicyDecision, Diagnosis}; event `policy_decided` |
| Verifier (`verifier`) | `verify_output` + draft + VerificationScope | Kiểm 8 invariant (mục 6). Chỉ được thu hẹp: bỏ ref/entity ngoài scope, dựng lại refund lines, hạ confidence; không thêm dữ kiện | `verification_passed` / `verification_repaired`; event `verification_completed` |

**Quyền gọi tool.** Allow-list nằm trong `agents.py`; `Specialist.fetch` ném `PermissionError` khi gọi tool ngoài danh sách. Tool không có trong kết quả `list_tools()` thì không bao giờ được gọi.

| Actor | Tool MCP được phép |
| --- | --- |
| coordinator | không có |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_order_payments`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| verifier | không có |

`get_customer_history` và `get_product_context` có trong danh sách tool của gateway nhưng không thuộc allow-list nào: L3A không cần dữ liệu khách hàng hay sản phẩm, nên workflow không kéo thêm dữ liệu cá nhân.

**Thứ tự chẩn đoán của policy-agent** (`rules.diagnose`, deterministic). Topic trong claim của khách hàng *không* phải input của bước này.

1. `order_status` là `canceled` / `unavailable` và có capture hợp lệ → `canceled_order_paid` / `unavailable_order_paid` (basis = tổng capture).
2. Giao sau ngày dự kiến (theo `get_order`) → nếu handoff cho carrier muộn hơn shipping limit muộn nhất thì `late_delivery_seller`, ngược lại `late_delivery_logistics` (basis = phí vận chuyển, không vượt số đã capture). Chỉ khi thiếu mốc handoff mới dùng actor của event `delivered_late` khớp ngày giao.
3. Refund event `failed` trong phạm vi → `refund_failed`; `pending` → `refund_pending`.
4. `reconciliation_mismatch` trong 24 h sau một capture → `payment_mismatch`.
5. Cùng số tiền bị capture hai lần ở hai thời điểm, với hai payment sequence khác nhau, và tổng vượt giá trị đơn → `duplicate_charge`.
6. Từ hai capture trở lên, cộng đúng bằng giá trị đơn → `valid_split_payment`.
7. Còn lại → `unsupported_claim`. Thiếu evidence bắt buộc (order, items, payment timeline, shipment, policy) → `insufficient_evidence`.

## 3. A2A protocol

Bus chạy in-process (`a2a.py`), mỗi case một bus. Envelope `A2AMessage` là frozen dataclass:

| Field | Ý nghĩa |
| --- | --- |
| `message_id` | `a2a-<case_id>-<nn>`; reply dùng `<message_id>-reply` |
| `case_id` | Case sở hữu message |
| `sender`, `recipient` | Actor gửi và actor nhận |
| `intent` | Tên task hoặc kết quả (`collect_order_context`, `payment_context_ready`, `task_failed`, …) |
| `payload` | Dataclass có kiểu (OrderFacts, ItemView, PaymentView, PolicyDecision, …) |
| `evidence_refs` | Các ref đã admit mà message dựa vào (đã khử trùng lặp) |
| `hop`, `reply_to` | Số hop và `message_id` của task được trả lời |

Quy tắc:

- **Chỉ coordinator giao task** (`A2ABus.request`). Specialist chỉ trả lời bằng `task.reply(...)`: hàm này đảo sender/recipient, gán `reply_to` và tăng `hop`.
- **Correlation theo `case_id`.** `_check_reply` từ chối reply không phải `A2AMessage`, khác `case_id`, sai `reply_to` hoặc sai peer; reply đó thành `task_failed` với lý do `invalid_reply`.
- **Điều kiện handoff.** Payment và shipment chỉ nhận task khi order-agent trả về OrderFacts hợp lệ (có anchor). `confirm_responsible_seller` chỉ chạy khi policy có party `seller`. Verifier luôn chạy. Specialist tự kiểm tra payload và trả `missing_order_anchor` nếu thiếu.
- **Timeout.** Mỗi task có 240 s (`asyncio.wait_for`), quá hạn → `task_failed` (`timeout`). Exception bên trong specialist → `task_failed` (`agent_error:<Type>`), để một case lỗi không làm hỏng cả lần chạy.
- **Chống vòng lặp.** Mỗi cặp (recipient, intent) chỉ được giao một lần trong một case; tối đa 12 task mỗi case (case bình thường dùng 5–6). Vi phạm ném `A2AProtocolError`.
- **Trace.** Mỗi task sinh `task_assigned` (actor = sender, target = recipient, `decision_code` = intent, attributes `message_id`, `hop`). Mỗi reply sinh `handoff` (actor = specialist, target = coordinator, `decision_code` = intent của reply, `evidence_refs`, attributes `message_id`, `reply_to`, `hop`, `evidence_count`, thêm `failure_reason` khi thất bại). Trace chỉ ghi decision code, bộ đếm và ref, không ghi nội dung suy luận.

## 4. Evidence lifecycle

1. **Discovery.** `discover_tools` gọi `list_tools()` một lần cho mỗi gateway session; tên tool không bao giờ được đoán.
2. **Call.** `gateway.call(tool, case_id=<case đang chạy>, order_id=<claimed_order_id>)`; riêng `get_policy` nhận `policy_version` của case.
3. **Validate envelope** (`mcp_gateway.py`). Kết quả lỗi của MCP → `RuntimeError`. `structured_content` phải khớp `mcp-evidence-response-v1` (pattern `evidence_ref`, `result_hash` sha256, domain trong enum, không có field lạ), nếu không → `ContractError`.
4. **Admit vào ledger.** `EvidenceLedger` được tạo mới trong mỗi `solve_case`. Domain phải đúng với tool, `evidence_ref` phải là chuỗi chưa từng xuất hiện, và `data.order_id` (nếu có) phải là order của case; sai thì ném `EvidenceRejected` (`out_of_scope_evidence`). Ref được lưu nguyên văn, không bao giờ được tạo ra hay sửa.
5. **Emit `tool_result_consumed`** cho mọi lần gọi (actor = specialist, kèm `tool_name`):
   - thành công: decision code `evidence_admitted` kèm ref;
   - thất bại: `not_found`, `timeout`, `invalid_evidence`, `out_of_scope_evidence` hoặc `tool_not_discovered`, không kèm ref.

   Attributes gồm `domain`, `rows`, `warnings`, `attempts`.
6. **Dùng.** Rule chỉ đọc `data` của record đã admit và luôn neo vào order row của `get_order`. Dòng nằm ngoài cửa sổ thời gian, thuộc order khác hoặc bị nhân bản sẽ bị loại và ghi vào `data_conflicts`.
7. **Map vào output.** `CITATIONS[issue]` liệt kê các tool mà kết luận dựa vào; `evidence_refs` của output là ref của những tool đó đã được admit. Cách xử lý claim:
   - **Topic là một policy issue.** Claim trích toàn bộ ref của output. Verdict `supported` khi evidence xác nhận đúng issue đó và case cần hành động. Verdict `unsupported` khi evidence chỉ ra issue khác, hoặc kết luận là không cần hành động (`valid_split_payment`, `unsupported_claim`). Verdict `insufficient_evidence` khi thiếu evidence.
   - **`requested_full_refund`.** Claim chỉ trích ref liên quan tiền (items, payment, refund, policy), và verdict theo action:
     - `issue_refund`, `retry_refund` → `supported`;
     - `refund_freight`, `refund_duplicate_charge`, `reconcile_payment` → `partially_supported`;
     - `monitor_refund`, `manual_review` → `insufficient_evidence`;
     - `document_no_action` → `unsupported`.
   - **Topic lạ.** Claim không kèm ref và nhận `insufficient_evidence`.
8. **Verify.** Verifier bỏ mọi ref không thuộc ledger của case và buộc ref của claim ⊆ ref của output.

Ledger bị hủy khi case kết thúc nên evidence không thể dùng lại giữa các case. Customer message và claim không bao giờ sinh ra ref, ID hay số tiền.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có, tối đa 2 lần gọi (1 retry), mỗi lần 90 s. Chỉ retry khi timeout; mọi call đều read-only nên idempotent | Coi như thiếu evidence. Thiếu evidence bắt buộc → `insufficient_evidence`, `manual_review`, refund 0, confidence 0.2 | `tool_result_consumed` / `timeout`, `attempts=2` |
| Not found / lỗi MCP | Không, vì lỗi xác định và gọi lại không đổi kết quả | Thiếu evidence như trên. `get_refund_timeline` not found là bình thường (đơn không có refund) và không trừ confidence. `get_sellers` not found → giữ seller từ item row, confidence −0.1 | `tool_result_consumed` / `not_found` |
| Source conflict | Không | `get_order` là nguồn authoritative. Dòng ngoài cửa sổ bị loại, dòng nhân bản bị gộp, refund bị cap theo số tiền thực sự đã capture. Ghi `data_conflicts` (tối đa 5, chỉ cho tool được trích dẫn) | `data_conflicts[].resolution_code` (danh sách bên dưới) |
| Invalid specialist result | Không; loop guard cấm giao lại cùng intent | Findings không đủ → `insufficient_evidence` + `manual_review`. Verifier lỗi → coordinator chạy `verify_output` cục bộ. Draft vẫn sai schema → output tối thiểu `insufficient_evidence` | `handoff` / `task_failed`, `failure_reason` (`timeout`, `invalid_reply`, `agent_error:<Type>`, `missing_order_anchor`, …) |
| Envelope sai schema hoặc sai scope | Không | Bỏ envelope, không bao giờ trích dẫn | `tool_result_consumed` / `invalid_evidence`, `out_of_scope_evidence` |

Các `resolution_code` dùng trong `data_conflicts`:

| Code | Trường hợp |
| --- | --- |
| `EXCLUDED_OUTSIDE_APPROVAL_WINDOW` | Event trong payment timeline nằm ngoài cửa sổ approval |
| `KEPT_ROWS_MATCHING_IN_WINDOW_CAPTURES` | Chỉ giữ payment row khớp capture hợp lệ |
| `COLLAPSED_REPLICATED_ROWS` | Gộp các dòng nhân bản |
| `EXCLUDED_OUTSIDE_ORDER_WINDOW` | Item row có shipping limit ngoài cửa sổ của đơn |
| `EXCLUDED_OUTSIDE_CASE_WINDOW` | Refund event ngoài khoảng từ lúc thanh toán đến `opened_at` |
| `EXCLUDED_NOT_MATCHING_DELIVERY` | Event shipment không khớp ngày giao thực tế |
| `PREFERRED_AUTHORITATIVE_ORDER_ROW` | Field của shipment lệch với order row; dùng order row |
| `PREFERRED_HANDOFF_VS_SHIPPING_LIMIT` | Actor trễ giao trong event lệch với so sánh handoff/shipping limit; dùng so sánh này |
| `CAPPED_TO_EVIDENCE` | Refund bị giới hạn theo số tiền đã capture |

Missing evidence không bao giờ được thay bằng giá trị phỏng đoán: không có order row thì `order_ids` rỗng, không có capture thì không có refund.

## 6. Verification invariants

Verifier (`verifier.py`) chạy 8 kiểm tra trên bản sao của draft trước khi finalize:

| # | Invariant | Kiểm tra / sửa |
| --- | --- | --- |
| 1 | `case_scope` | `case_id` và `schema_version` khớp case đang chạy và `day09-l3a-output-v2` |
| 2 | `evidence_ownership` | Mọi `evidence_refs` thuộc ledger của case, không trùng lặp |
| 3 | `claim_linkage` | Ref của từng claim ⊆ `evidence_refs` của output |
| 4 | `entity_scope` | `order_ids` chỉ gồm order được `get_order` xác nhận; `item_ids` và `seller_ids` ⊆ các item row trong phạm vi; `party_id` của seller phải là seller của case (ID ví dụ trong policy bị loại) |
| 5 | `money_totals` | Refund ≤ mức của policy và ≤ tổng đã capture; bằng 0 với `document_no_action` / `monitor_refund`; tổng `refund_lines` = `recommended_refund_brl` |
| 6 | `policy_consistency` | `primary_issue`, `case_status`, `resolution_actions` khớp PolicyDecision |
| 7 | `confidence_bounds` | Trong [0, 1]; ≤ 0.4 với `insufficient_evidence`; −0.1 nếu có bất kỳ sửa chữa nào |
| 8 | `schema` | Validate theo `l3a-output-v2` |

Kết quả được ghi vào event `verification_completed`: decision code `passed` hoặc `repaired`, attributes `checks_run`, `repairs`, `repair_codes`, `primary_issue`, `confidence`, `evidence_count`. CLI validate schema và `case_id` thêm một lần trước khi ghi file.

**Calibration.** Confidence bắt đầu từ 0.95 và bị trừ 0.1 cho mỗi điều sau:

- mỗi nghi vấn trong chẩn đoán: capture không có payment row, shipment lệch order row, actor trễ giao không rõ hoặc bị lệch, không có capture trong cửa sổ;
- refund bị cap dưới mức policy;
- seller không được registry xác nhận.

Kết quả được kẹp trong [0.05, 0.99]. Case `insufficient_evidence` được đặt 0.2.

## 7. Reproducibility

- **Model/config.** Không dùng LLM, không có temperature hay random seed. Mọi quyết định là rule deterministic trong `rules.py` cộng với policy lấy từ `get_policy`. Giá trị duy nhất không cố định là `evidence_ref` do server cấp và `event_id`/timestamp của trace.
- **Runtime.** Python ≥ 3.11, đã chạy với CPython 3.13.9. Dependency được giới hạn theo major version trong `pyproject.toml`. Các phiên bản đã kiểm tra: `mcp` 2.2.0, `httpx2` 2.13.1, `jsonschema` 4.25.1, `python-dotenv` 1.2.2, `pytest` 8.4.2, `ruff` 0.16.9. `mcp` 2.x trả field theo snake_case (`is_error`, `structured_content`); `mcp_gateway.py` đọc được cả hai kiểu tên.
- **Concurrency và giới hạn.** Các case chạy tuần tự trong một MCP session. Trong một case có tối đa 4 call MCP song song (3 của payment-agent + 1 của shipment-agent). Mỗi case dùng 7–8 call: 7 tool lõi, thêm `get_sellers` khi seller chịu trách nhiệm. Các timeout:
  - HTTP: read 300 s, connect 30 s;
  - MCP call: 90 s × 2 lần;
  - A2A task: 240 s, tối đa 12 task mỗi case.
- **Lệnh chạy.**

  ```bash
  python -m pip install -e ".[dev]"
  cp .env.example .env   # điền COMPETITION_API_URL, COMPETITION_TEAM_API_KEY, MCP_ENDPOINT
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  python -m ruff check . && python -m pytest -q
  ```

- **Kiểm thử.** `tests/test_workflow.py` chạy offline với gateway giả: envelope tổng hợp, không dùng mạng, không dùng `inputs/`. Các test phủ:
  - luồng end-to-end;
  - cap refund;
  - thiếu order;
  - retry khi timeout;
  - ledger từ chối evidence ngoài scope;
  - guard của bus;
  - phân biệt split payment với duplicate charge.

  `tests/test_release_safety.py` chỉ pass trên cây release sạch, không có `case-set.json` và `inputs/`.
- **Bí mật.** API key chỉ được đọc từ `.env` (đã gitignore) và chỉ gửi qua header Bearer. Key không được log, không ghi vào output hay trace; `day09 validate` quét pattern `sk-team-`. ZIP nộp bài chỉ chứa `manifest.json`, `trace.jsonl` và `outputs/<case_id>.json`.
