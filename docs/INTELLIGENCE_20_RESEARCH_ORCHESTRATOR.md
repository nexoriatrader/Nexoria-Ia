# NEXORA INTELLIGENCE 2.0 — Orquestrador de pesquisa I01/I02/I03/I06

## Objetivo

Uma avaliação centralizada, compartilhável e rastreável por ativo,
sem uma segunda infraestrutura de coleta e sem execução financeira.

**Fluxo real e único:**

`asset_scanner.scan_intelligence_research()`
→ `scan_intelligence_context()`
→ `scan_intelligence_markets()` (I02 sobre candles públicos I01)
→ `timeframe_engine.enrich_i02_report()` (I03)
→ `research_signal_engine.assess_research_report()` (I06)
→ `research_orchestrator.consolidate_research_report()`.

O coordenador reutiliza o retorno I06. A função
`orchestrate_public_research(previous=..., prior_i06=..., expiry_minutes=..., now=...)`
chama a **fachada existente uma única vez**. Não reexecuta indicadores,
não cria scanner, cache, feed, web socket, worker, agendamento, tabela
ou endpoint; o cache existente da aplicação continua sendo o único da
família de relatórios públicos.

## Contrato público de pesquisa

`consolidate_research_report(i06_report, now=...)` recebe
`intelligence2_i06_evidence_research_v1` e devolve
`intelligence2_research_orchestrator_v1`, com:

- `assets`: uma avaliação consolidada por par `(market, symbol)`;
- `source_versions`: versões efetivamente consumidas de I02/I03/I06
  (I01 entra pela proveniência pública verificada por I02 e I03);
- `state`: `RESEARCH_CANDIDATES`, `WAIT_RECHECK`,
  `NO_VALID_RESEARCH`, `SOURCE_EXPIRED` ou `SOURCE_UNTRUSTED`;
- estado individual: `RESEARCH_READY` (somente dados públicos adequados
  para **estudo**, nunca para compra), `WAIT_RECHECK` ou `EXCLUDED`;
- `observed_trend`: UP/DOWN/NEUTRAL da I06 (não CALL/PUT);
- `evidence_completeness_points`: pontuação **já produzida por I06**,
  não recalculada: completude de cinco verificações de dados entre 0–100;
- `fingerprint`, validade, justificativas, estado do lifecycle e contagens;
- `i04`/`i05` explicitamente `NOT_CONNECTED`;
- `distribution.publish_allowed=false`, `execution_allowed=false`,
  `approved_for_trading=false`, `orders_sent=0`.

Não são retornados dados de usuários, contas, tokens, saldo ou ordens.

## Regras de consolidação e contenção

- Exige versão/proveniência I06, origem I02/I03, flags de execução
  bloqueadas e relatório público limitado ao `MAX_ITEMS` já existente.
- Vencimento do relatório ou do registro elimina prontidão para estudo.
- Qualidade incompleta, candle aberto/atrasado, conflito I03 e dados
  faltantes propagam `EXCLUDED` ou `WAIT_RECHECK` com motivos.
- `RESEARCH_READY` exige score de **completude** 100, todos os cinco
  pré-requisitos, evidência coerente, padrão público observado, ciclo
  ativo, candle atual e tendência consensual. Não é aprovação de contrato.
- Um registro inalterado exige rechecagem, não nova publicação.
- Repetições do mesmo ativo são suprimidas e invalidadas.
- Campos falsificados de compra, expiração, momento de entrada,
  autorização financeira ou publicação rejeitam o item.
- Somente campos públicos conhecidos são copiados para a resposta.

## Limites e dependências

- Este orquestrador não está exposto em novo endpoint e não será
  ativado em produção por esta PR.
- I04 Knowledge Base, I05 Learning, Strategy Intelligence dedicado e
  Signal Distribution não estão integrados; não existem interfaces
  inventadas.
- Deduplicação entre chamadas depende do estado público fornecido
  ao motor I06; não existe persistência ou consenso multiprocesso.
- A qualidade é heurística e descritiva. A pontuação não representa
  probabilidade de sucesso, rentabilidade ou poder preditivo validado.
- Não há sinais CALL/PUT, janela de compra, expiração de contrato,
  aprovação para execução ou envio à Deriv.

## Testes e entrega

`tests/test_intelligence2_research_orchestrator.py` cobre cadeia com
I06 real em memória, uma única chamada à fachada existente, tendência,
falta de dados, conflitos, candle não fechado, expiração, ausência de
oportunidades, lifecycle, duplicidade, fontes adulteradas, privacidade,
limite de capacidade, clock inválido e proibição de publicação.

A branch do orquestrador parte da PR #59, formalmente
`I06 — Research Evidence Engine`. A PR de orquestração deve apontar
para essa branch, preservando o diff isolado. **DRAFT; sem merge,
deploy, migração, mudança de flags ou execução financeira.**
