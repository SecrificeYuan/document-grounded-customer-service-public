修复上一份 AnalysisResult，使其同时满足 JSON Schema 和给出的确定性校验错误。
业务文档仍是唯一事实来源；不得编造 ID、条件、数值、单位或引文，不得增加未列入 claim_plan 的承诺。
修复前重新核对每个核心意图：询问自己的设备、订单或账号能否办理业务的是 case_application，即使条件尚缺，也不得改成 rule_description 并罗列所有规则分支。多个并列核心意图只要有一个案例判断缺少影响唯一结论的条件，就列出对应 missing_field_ids，整题 handoff + AMBIGUOUS，清空 claim_plan，不保留其他意图的部分回答；纯一般规则询问不受此限制。
先判断用户是否提出了具体要办理、判断或查询的事项，再判断该事项的文档覆盖情况。若只指出对象或业务主题并问“怎么办”“怎么处理”，没有明确具体事项，应将 intent.scope 修为 uncertain、清空 claim_plan，并使用 handoff + AMBIGUOUS；不得从契约主题名推测用户在问折扣、账期、退货等某项政策。此优先级高于 in_scope_uncovered 的证据不足规则。
只有具体请求已明确且最具体匹配的 scope topic disposition 为 in_scope_uncovered 时，intent 才保持 in_scope，清空 family_ids、claim_ids、selected_unit_ids 和 claim_plan，并使用 handoff + INSUFFICIENT_EVIDENCE；不得把“文档未规定或未承诺”修成 answer。
validation_errors 只是校验结果，不是新的业务事实。previous_final_json 仅供定位错误。
修复 FACT_SOURCE_MISMATCH 时，每个 extracted_fact 的 source 必须缩减为能够独立解析为对应 typed value 的最小原文片段。日期事实只保留日期表达本身，不得包含渠道、动作或其他相邻文字；value 必须与该片段可验证地一致。
修复 FACT_SOURCE_MISMATCH 时，布尔事实的 source 必须逐字对应契约中的字段名称、别名或这些表达的显式否定；不得根据契约未注册的反义词、近义词或常识推断 true/false。删除无法精确映射的 extracted_fact；仅当该表达影响结论时才改列为 unresolved_expression 和 missing_field_id。
修复枚举 FACT_SOURCE_MISMATCH 时，source 必须逐字对应 allowed_values 中的 code、display_name 或 alias；契约登记复合 alias 时使用完整复合表达，不得截成更宽泛的相邻词。
scope 表示用户实际请求的结论范围，而不是文档是否记载了拒绝或转人工规则。用户询问客服如何处理专业事项时可以回答来源支持的处理规则；用户直接要求生成专业意见、判断或预测时属于 out_of_scope，使用 handoff + OUT_OF_SCOPE，不得把拒绝说明 Claim 修成 answer。
业务主题虽可识别，但用户没有说明要办理、判断或查询的具体事项时，intent.scope 使用 uncertain，清空 claim_plan，并使用 handoff + AMBIGUOUS；不得改成 INSUFFICIENT_EVIDENCE。
每个 claim_plan 项必须与同一 intent 对齐：intent 的 family_ids 包含 Claim 所属 Policy Unit 的 family_id，claim_ids 包含该 claim_id。
修复 MISSING_COMPANION_CLAIM 时，从 validation_errors 指定的 Claim 开始递归展开 required_companion_claim_ids，并把全部伴随 Claim 放入同一 intent 的 claim_ids、selected_unit_ids 和 claim_plan；每项使用自己的 canonical_text 和证据，不得删除父 Claim 规避错误。
每个 intent 的 family_ids 只包含直接回答该意图所必需的规则族。问题中的背景时段、渠道或状态只用于判断条件时，不得据此保留额外规则族或 Claim。
每个 assertion_text 必须逐字使用所选 Claim 的 canonical_text，不得改写、合并或增删；proposed_decision 为 answer 时，answer_draft 必须完全等于 claim_plan 中 assertion_text 按顺序拼接的结果。
proposed_decision 为 handoff 时，不规划部分回答，claim_plan 必须为空，answer_draft 固定为“已转人工处理。”；不得把空字符串作为 answer_draft。
修复 CLAIM_LITERAL_MISMATCH 或 ASSERTION_CLAIM_MISMATCH 时，如果 Claim 有原文支持且直接回答对应 intent，不得仅为清除错误而删除该 Claim；应保留 claim_id，并把 assertion_text 替换为该 Claim 的完整 canonical_text，同时使用该 Claim 自己的证据。
每个 quote.fragment 必须是 quote.evidence_id 所指 evidence block 文本中的逐字连续子串；不得把一个证据块的文字标成另一个 evidence_id，也不得根据 canonical_text 改写或拼接引文。
修复引文时，每个所选 Claim 的全部 quote.fragment 合起来必须完整覆盖该 Claim 的全部数值和单位，不得包含该 Claim 未登记的其他数值和单位，并保留否定和限定；同一证据块同时含有新旧数值时，只截取支持当前 Claim 的逐字连续片段，不得用冲突语句或另一个 Claim 的引文代替。
输出完整、替换用的 AnalysisResult JSON，不输出解释或思维链。
