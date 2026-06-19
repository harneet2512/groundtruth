# Failure Taxonomy

## A. Substrate/Index
- A1: graph missing
- A2: graph stale
- A3: assertion linking wrong
- A4: edge confidence wrong
- A5: property extraction wrong
- A6: incremental reindex corrupts graph

## B. Path/Node Resolution
- B1: host/container path mismatch
- B2: basename collision
- B3: LIKE escaping bug
- B4: node ambiguous
- B5: wrong node selected
- B6: path resolver duplicated/inconsistent

## C. Trigger Detection
- C1: edit not detected
- C2: view not detected
- C3: grep not detected
- C4: finish not intercepted
- C5: scaffold not detected
- C6: baseline gate wrong

## D. Evidence Generation
- D1: empty evidence
- D2: wrong evidence
- D3: noisy evidence
- D4: over-budget truncation
- D5: wrong ranking
- D6: missing required evidence family

## E. Delivery Routing
- E1: marker mismatch
- E2: dedup false suppression
- E3: hidden filter removed evidence
- E4: stuck-compat false suppression
- E5: router returned before valid legacy path
- E6: payload generated but not inserted into observation

## F. Visibility/Actionability
- F1: inserted after agent step
- F2: finish evidence too late
- F3: visible but buried
- F4: visible but misleading
- F5: visible but agent ignored
- F6: agent-visible evidence differs from logged evidence

## G. Metrics/Observability
- G1: event says delivered but output lacks evidence
- G2: output has evidence but metrics missed it
- G3: path mismatch in metrics
- G4: duplicate counting
- G5: marker-only false positive
- G6: trajectory parser skipped relevant text

## H. MCP/Tool Integration
- H1: tool registered but unreachable
- H2: tool reachable but output not useful
- H3: passive equivalent missing
- H4: tool action path untested
- H5: tool claims not reflected in hook behavior
