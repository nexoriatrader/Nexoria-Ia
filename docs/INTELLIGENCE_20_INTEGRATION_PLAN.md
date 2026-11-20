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
| **I03 — IMPLEMENTADA** | Context + Timeframe: reutilização de EMA/RSI, padrões de swings e candles fechados I01; enriquecimento da seleção I02 | I01/I02 | Testes de tendência, volatilidade, transição, conflito/reversão, fechamento, isolamento, não-execução e CI |
| **I04 — IMPLEMENTADA** | Knowledge Base persistente: 5 categorias públicas, evidências, estados, versões, revisão e log em PostgreSQL existente | I03 | Testes SQLite sintéticos + PostgreSQL 16 descartável; migração formal preparada, não aplicada em produção |
| **I05 — PLANEJADA** | Learning Engine utilizando Knowledge Base versionada; sem treinamento ou promoção automática nesta etapa | I04 | Validação, rollback e critérios de treinamento dependem de escopo e autorização futuros |
| **I06 — A REPLANEJAR** | Strategy Intelligence + Priority + Distribution estritamente informativa | I05 | Exige decisão e testes próprios; não iniciada |
| **I07+ — A REPLANEJAR** | Orchestrator, Risk Guardian e Execution Reconciliation somente leitura, por etapas separadas | I06 | Sem permissão financeira ou instalação de serviços paralelos |
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

## I03 — Context Engine + Timeframe Engine: implementação desta branch

**Branch exclusiva:** `feature/intelligence20-i03-context-timeframe`, criada
no SHA final da I02 (`55f78f1d8564cfcde4d3609b4dc4708298058af6`).
PR #52, #51 e os seis P0/F01 são imutáveis nesta entrega.

### Reuso e integração sem arquitetura paralela

- `app/intelligence2/context_engine.py::analyze_context` é **puro**, toma
  exclusivamente o retorno de `intelligence2.market_data.read_closed_window`
  (I01) e reutiliza `automation_signals._ema`, `_rsi` e
  `trade_selector_v2.swing_structure` para evidências de EMA20/EMA50,
  RSI14, estrutura de swings, eficiência de tendência, dispersão estatística,
  tendência dos últimos fechamentos e anomalias de amplitude.
- `app/intelligence2/timeframe_engine.py::choose_hierarchy` define apenas
  perfis suportados por I01: `M15/M5/M1`, `H1/M5/M1` ou `H1/M15/M5`.
  A escolha depende da expiração de pesquisa (1, 3, 5, 15 minutos),
  volatilidade observada, regime, família da estratégia e mercado. Nenhuma
  duração contratual ou elegibilidade financeira é presumida.
- `align_timeframes` reúne os três contextos fechados e retorna
  `ALIGNED_RESEARCH`, `WAIT_REEVALUATE`,
  `BLOCKED_STRONG_CONFLICT`, `REVERSAL_CONFIRMED_RESEARCH`
  ou `INDETERMINATE`. Conflitos moderados aguardam; conflitos fortes
  bloqueiam o avanço exploratório. Reversão requer evidência maior
  explicitamente fraca, os dois timeframes menores concordantes,
  força/estabilidade/confiabilidade mínimas e duas velas **já fechadas**
  confirmando a mesma direção em CADA timeframe menor.
- A falha de uma leitura I01, OHLC inconsistente, lacunas, barra aberta,
  timestamps desencontrados, histórico insuficiente ou contexto irregular
  produz `INDETERMINATE`. **Nunca** compensa dados ruins com score.
- `asset_scanner.scan_intelligence_context` chama o scanner I02 já
  existente e depois `enrich_i02_report`. A única rota permanece
  `GET /api/research/asset-scanner?version=3&expiry_minutes=5`.
  A chamada tradicional (`version=1`) e I02 (`version=2`) não mudam.
- A própria rota autenticada conserva seu **único cache**/lock público.
  O novo perfil de expiração e o bucket do candle M1 fazem parte da chave
  efetiva da versão 3, evitando reaproveitar a mesma resposta em perfis
  diferentes ou depois de um novo minuto. Nenhum novo cache ou scheduler.
- Até oito ativos principais do ranking I02 por ciclo recebem contexto I03,
  em ordem intercalada entre mercados, máximo de duas avaliações
  concorrentes. Os demais retornam `NOT_EVALUATED_CAPACITY`. Essa
  limitação é documentada no payload; não é avaliação parcial disfarçada.
  As reservas seguem em monitoramento leve da I02, sem novos workers.
- Contratos `PENDING`/`UNKNOWN`/`CONFIRMED` DEMO/REAL e slots
  retidos continuam apenas visíveis em overlay separado **por usuário**,
  montado fora do relatório/cache público. I03 não altera nem concilia ordens.

### Contrato de saída e limitações de ML

Cada ativo avaliado contém `context_timeframe`, com `profile`,
`contexts.higher/intermediate/lower`, `alignment`, evidências, motivos,
força, estabilidade, confiança heurística não probabilística, versões,
timestamp, SHA-256 de proveniência **somente de dados públicos** e
`execution_allowed=false`, `approved_for_trading=false`.

A I03 não possui um modelo ML já validado e versionado a partir da base.
O campo `ml.status=NO_VALIDATED_MODEL_AVAILABLE`/`used=false` declara isso:
**não** há ajuste, inferência de ML, predição de vitória ou treinamento
oculto. Uma futura integração de modelos deverá requerer artefatos de
validação fora da amostra e autorização de escopo específica, sem
sinais/ordens financeiros.

**I04 ainda não foi iniciada**. A I03 foi limitada a classificação de
contexto e hierarquia temporal. A confiança é somente uma medida
determinística de consistência dos indicadores, nunca probabilidade
calibrada de lucro.

### Critérios de aceite e CI

`tests/test_intelligence2_context_timeframe.py` cobre tendências de alta
e baixa, lateralização, baixa/alta volatilidade, transição, irregularidade,
falhas e gaps, exclusão de candles abertos, perfis por mercado/volatilidade/
expiração, conflito forte/moderado, reversão com/sem confirmação, limites
de concorrência, cobertura parcial explícita, integração I01/I02,
respostas idênticas de contexto público em DEMO e REAL, contas isoladas
no overlay e ausência de compra. CI continua as suítes completas I01/I02,
P0-01…P0-05, F01 e PostgreSQL 16 descartável.

**Regra de preservação:** não há merge, deploy, migração em produção,
ativação de flags REAL, compra, novo executor, novo ledger, novo modelo
não validado ou início de I04.


## Atualização de escopo I04

A decisão vigente separa a I04 (Intelligence Knowledge Base) da I05 (Learning Engine).
A I04 é implementada em `app/intelligence2/knowledge_base.py`, usa as três
novas tabelas aditivas no banco existente, estado/versionamento, proveniência,
recuperação fail-closed e reaproveita `HistoricalResearchReport`. A migração
`migrations/I04_KNOWLEDGE_BASE_V1_POSTGRES.sql` é **somente proposta** e não
foi executada em produção. O processo existente de pesquisa incorpora revisão
periódica segura apenas após habilitação explícita do serviço.
A especificação e limites completos estão em
`docs/INTELLIGENCE_20_I04_KNOWLEDGE_BASE.md`.
**Não iniciar I05 automaticamente.**
