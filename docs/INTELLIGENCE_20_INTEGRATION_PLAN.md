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
| **I02 — IMPLEMENTADA** | Market Scanner: catálogo público dinâmico, ranking rastreável por mercado, 3 principais + 2 reservas | I01 | Testes de seleção, dados ruins, rotação, sessão, concorrência, continuidade de contratos e CI acumulado |
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

## I02 — scanner integrado: implementação desta branch

- **Ponto de entrada preservado:** `GET /api/research/asset-scanner` sem
  parâmetros mantém a resposta legada. `?version=2` ativa a I02 de modo
  explícito, na mesma rota autenticada e sob o mesmo lock/cache de resultado.
- **Reuso:** `asset_scanner.scan_intelligence_markets` delega à composição
  `intelligence2.market_scanner.scan_market_opportunities`. O scanner usa
  `automation_markets.market_instruments` (catálogo e cache existentes),
  `market.discover_contracts` (consulta pública existente),
  `intelligence2.market_data.read_closed_window` (I01 com
  `market.shared_candle_request` e `strict_closed_bars`),
  `asset_scanner.scanner_family` e `autopilot.analyze`. Nenhum
  websocket, scheduler, cache público adicional ou broker de compras.
- **Cobertura:** percorre TODOS os grupos informados no catálogo público,
  incluindo grupos novos identificados pelo campo `market` da Deriv.
  A quantidade de ativos por ciclo é **limitada a 8 por mercado** por
  controle de capacidade, com amostras rotativas a cada 5 minutos.
  O resultado declara `scan_coverage_complete=false` e
  `selection_scope=EVALUATED_SAMPLE_NOT_GLOBAL_MARKET_RANKING`
  quando há ativos adiados. Isso NÃO reivindica os três melhores absolutos
  do mercado sem avaliar todos os ativos.
- **Seleção:** até 3 `selected` e 2 `reserves` por mercado,
  ordenados por pontuação técnica reproduzível, com justificativas e partes
  da pontuação: candles completos, atualidade, volatilidade, estrutura
  direcional, compatibilidade exploratória e condições públicas CALL/PUT.
  Os ativos selecionados e as duas reservas anteriores são rechecados em
  cada nova leitura solicitada, enquanto as demais posições do catálogo
  rotacionam. **Monitoramento leve não é um loop nem execução autônoma.**
- **Recusas:** mercado não classificado, sessão explicitamente fechada,
  ativo suspenso, I01 indicando `INSUFFICIENT`/`GAPPED`/`STALE`,
  histórico insuficiente, variação ausente ou excessiva, tipo de contrato
  ausente, dados/consulta indisponíveis; sem nenhuma inclusão forçada.
- **Interpretação:** `RESEARCH_ONLY` e `NO_OPPORTUNITIES` falam da
  **amostra examinada**, não de toda a Deriv. O score é heurística de
  observação e **não valida retorno, payout, duração contratual, saldo,
  elegibilidade da conta nem possibilidade de execução**. Nenhum campo é
  autorização para enviar sinal à execução financeira.
- **Contratos:** após recuperar o resultado público em cache,
  `open_contracts_for_user` lê as ordens DEMO/REAL abertas ou com slot
  ainda retido, SEM escrever. O filtro por `user_id` evita cruzamento entre
  usuários e a leitura acontece FORA do cache público compartilhado.
  Trocar ativos observados não altera contratos, slots ou conciliação.
- **Pronto para I03:** cada ativo elegível apresenta `context_inputs`,
  `timeframe_inputs` e `strategy_inputs` somente descritivos.
  Context, Timeframe e Strategy Intelligence não foram implementados.
- **Testes:** `tests/test_intelligence2_market_scanner.py`,
  incluídos em job dedicado do CI, além do acervo completo I01, P0 e F01
  (com PostgreSQL 16 exclusivamente descartável).

**Próxima fase planejada:** I03 — Context Engine + Timeframe Engine, ainda
não iniciada. Sem merge, deploy, migração, flags REAL ou ordens.
