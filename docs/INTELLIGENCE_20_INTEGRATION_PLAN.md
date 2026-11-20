# NEXORA INTELLIGENCE 2.0 — roteiro de integração incremental

**Origem imutável:** `64f27c3c882b4d43d1a1d46d996b52acb27c46a8` (F01 / PR #50).
**Branch nova:** `feature/intelligence20-market-data-readonly`.
**Política:** evolução por composição dos serviços existentes; nenhuma migração, merge,
deploy, ordem, alteração de flags administrativas ou edição das PRs P0/F01.
Todas as conexões financeiras e estratégias de compra REAL permanecem fora deste roteiro.

## Mapa de reutilização — 14 módulos

| Módulo | Ativos existentes reaproveitados | Estado inicial e ação mínima |
| --- | --- | --- |
| Market Data Infrastructure | `app/market.py::public_request`, `shared_candle_request` e `public_feed_messages`; `shared_market_store.PublicCandleSnapshot`, `get_snapshot`, `save_snapshot` | **Existente + integração I01.** Criar somente facade de leitura/qualidade `intelligence2.market_data.read_closed_window`; NÃO duplicar WebSocket/cache/DDL |
| Market Scanner | `asset_scanner.scan_assets`, `choose_assets`; `strategy_lab.fetch_candles` | **Existente; integrar depois.** Consumir o contrato de qualidade do I01, mantendo pesquisa sem ordens |
| Intelligence Knowledge Base | `research_persistence.HistoricalResearchReport`, `latest_study`, `latest_multitimeframe_study`; `research_retention` | **Persistência já existe; integrar leitura/versionamento.** Evitar nova tabela até provar necessidade |
| Context Engine | `automation_markets.market_instruments`, catálogo `GROUPS`, `status_contract.contract_summary` | **Parcial.** Compor contexto somente de leitura com origem e estado desconhecido explícitos |
| Timeframe Engine | `trade_selector_v2.strict_closed_bars`, `multi_timeframe_study.aggregate` | **Existente; integrar.** Consumir candles fechados, rejeitar gaps e intervalos incompletos |
| Strategy Intelligence Engine | `strategy_lab.evaluate`, `multi_timeframe_study.analyze_m1`, `smc_study.make_smc_signals` | **Pesquisa existente; integrar resultados históricos sem prometer lucratividade** |
| Signal Generation Engine | `automation_signals.confirmed_automation_signal`, `pulso_live.confirmed_pulso_signal` | **Lógica existente ligada a automação legada.** Não duplicar; nenhuma nova ligação à execução nesta fase |
| Signal Priority Engine | `asset_scanner.choose_assets`, critérios existentes em `trade_selector_v2` | **Parcial.** Futuro ordenamento explicável de achados de pesquisa, nunca comando de compra |
| Signal Distribution Engine | `main.py` endpoints de pesquisa e `status_contract.evaluate_user_status` | **Leitura já existe; integrar apresentação.** Não criar fila de ordens, serviço de dispatch ou WebSocket paralelo |
| Risk Guardian | `mode_settings.settings_for`, `AccountRiskSettings`, `real_ledger.validated_real_stop_loss`, `order_integrity.audit_real_account` | **Controles existentes.** Reutilizar para diagnóstico/documentação, sem duplicar regras ou alterar gates REAL |
| Execution Engine | `demo_ledger`, `real_ledger.reserve_real_order`, `real_runtime._user_scan`, `restart_consistency.locked_real_consent_for_send` | **Existe e permanece congelado.** Nenhuma integração do Intelligence 2.0 com compra REAL, nem nova rota de envio |
| Execution Reconciliation | `real_recovery.promote_stale_pending_to_unknown`, `account_recovery_summary`, `real_runtime.reconcile_real_once`; `RealOrder` | **Existente.** Leitura de estados PENDING/UNKNOWN/CONFIRMED e contratos abertos; sem reenvio/liberação de slots |
| Learning Engine | `strategy_lab.score`, `multi_timeframe_study.score`, `HistoricalResearchReport` | **Aprendizado exploratório parcial.** Reusar holdout cronológico, proveniência e limitações; não treinar execução automática |
| Orchestrator | `main.lifespan`, `research_persistence.research_loop`, `real_runtime.real_loop`, `status_contract` | **Ciclo de vida existente.** Não criar scheduler paralelo; integrar somente leitura conforme interfaces fiquem estáveis |

### Proteções transversais que não serão recriadas

| Área | Fonte existente | Regra de composição |
| --- | --- | --- |
| Isolamento DEMO/REAL | P0-01; `AccountRiskSettings` por usuário/modo/conta | Não misturar preferências, ordens, relatórios ou resultados |
| Idempotência | P0-03; `real_replay_matches`, `demo_replay_matches`, constraints do ledger | Reutilizar funções existentes quando for seguro revisar dados; não criar outro ledger |
| Concorrência | P0-03; CAS, unicidade, locks PostgreSQL | Não construir transações paralelas concorrentes |
| Fencing e STOP | P0-04 + F01; `RealGlobalAuthorizationFence`, `RealAuthorizationFence`, `RealRobotConsent` | Não tocar no código de execução/revogação, nem alterar a ordem de locks |
| Recuperação | P0-02; `real_recovery` | Nunca converter UNKNOWN em sucesso ou liberar slot por prazo |
| Capacidade/observabilidade | P0-05; `status_contract`, limites em `market.py` | Estado desconhecido continua desconhecido; cache e backpressure permanecem centralizados |
| Migração | F01; `nexora_schema_revisions` | Startup PostgreSQL sem DDL; não criar novas tabelas nesta integração |

## Plano de execução por prioridade e critérios verificáveis

| Prioridade | Integração / entrega | Dependência | Aceite verificável |
| --- | --- | --- | --- |
| **I01 — AGORA** | Market Data Infrastructure: facade interna de candles fechados e qualidade, sem cache próprio | `market.shared_candle_request` + `strict_closed_bars` | Testes mocks de source, fechamento, OHLC, tempo, duplicidade, gaps, sem compra; CI geral + PG da base |
| **I02** | Market Scanner read-only: adaptar `asset_scanner` ao contrato I01 preservando API e payload legados | I01 | Testes scanner/contagens + falha parcial, somente dados sintéticos, sem chamadas financeiras |
| **I03** | Context + Timeframe: composição de símbolos e agregados reutilizando `automation_markets` e `multi_timeframe_study` | I01/I02 | Testes alinhamento M1/M5/M15/H1, sem look-ahead, rejeição de gaps |
| **I04** | Knowledge Base + Learning: consulta versionada de pesquisa persistida | I03 | Testes de integridade/proveniência, ausência de overwrite financeiro, sem nova migração |
| **I05** | Strategy Intelligence + Priority + Distribution somente informativa | I04 | Comparações históricas com holdout e rótulos de hipótese; nenhum gatilho de ordem |
| **I06** | Orchestrator: compor na rotina de pesquisa existente, sem novos loops nem filas | I05 | Testes de cancelamento, limite de concorrência, degradação e inatividade segura |
| **I07** | Risk Guardian + Execution Reconciliation: vistas de estado existentes, sem mutação | I06 | F01/P0 regressões; UNKNOWN preservado; nenhuma autorização nem reserva criada |
| **Fora de escopo** | Ampliação do Execution Engine para negociação REAL | Nenhuma | Proibida nesta fase |

### I01 — implementação restrita desta branch

`app/intelligence2/market_data.py::read_closed_window` delega exclusivamente a
`market.shared_candle_request` (que já utiliza conexões públicas limitadas,
deduplicação em voo e cache opcional) e a
`trade_selector_v2.strict_closed_bars` (regras de OHLC e candle fechado).
A facade devolve `quality=COMPLETE|INSUFFICIENT|GAPPED|STALE`,
`missing_intervals`, época esperada, e `usable_for_research`.
Mesmo com qualidade completa, `execution_allowed=false` sempre.

Não há endpoints novos, worker, cache, DDL, dependências, credenciais ou
alteração nos serviços existentes. A função não gera ou distribui sinais.
`tests/test_intelligence2_market_data.py` usa somente respostas sintéticas.
O GitHub Actions executa a suíte específica e a regressão da base.

### Itens operacionais herdados, sem reabrir as P0/F01

- A confirmação de flags, migração formal de produção, topologia e plano de
  convivência entre versões continua pendente para qualquer implantação futura.
- O bloqueio final de consentimento pode manter uma transação aberta durante
  chamada externa; essa observação **não** altera a implementação nesta branch.
- `UNKNOWN` sem contrato permanece indisponível para reenvio automático.
- APIs legadas de configuração e clientes de respostas HTTP 207/`running=null`
  exigem inventário de compatibilidade, não alteração da execução agora.
- Sem heartbeat global não se declara shutdown físico de todos os workers.

**Decisão:** I01 é o único item implementado nesta branch; I02–I07 são etapas
sequenciais propostas, não executadas. Cada etapa exige testes próprios,
manutenção dos gates financeiros desativados e autorização de continuidade.
