# NEXORA Intelligence 2.0 — I06: avaliação de evidências, pesquisa somente

## Implementado

O I06 reutiliza exatamente a cadeia `I01 -> I02 -> I03` já integrada no
`app/asset_scanner.py`. Não abre feed, WebSocket, tabela, worker ou cache
independente. Consome somente resultados **públicos**, descartando qualquer
uso de sessão, conta, token ou saldo na decisão.

O motor `app/intelligence2/research_signal_engine.py`:
- exige proveniência de I03, guards não executáveis, I02 conhecido e dados I01 completos;
- registra justificativas por código, classificação **qualitativa** da evidência,
  timestamp, validade (60 segundos) e impressão SHA-256;
- identifica repetição entre observações, substituição e retirada de itens;
- verifica a âncora de candle fechado do I02: ausente, futuro ou mais de
  120 segundos defasado => pesquisa inválida e `DO NOT TRADE`;
- descreve a tendência observada do I03 apenas como `UP`, `DOWN` ou
  `NEUTRAL` e exige consenso de três timeframes para uma descrição
  direcional. Não traduz tendência para CALL/PUT ou instrução de contrato;
- calcula força de evidência em classes `CONSISTENT`, `INCONCLUSIVE`,
  `INVALID`, sem usar magnitude do score como chance de ganho;
- acompanha `ACTIVE_RESEARCH`, `EXPIRED`, `CANCELLED` e
  `CONCLUDED` como estados de registro de pesquisa, preservando
  NEW/UNCHANGED/UPDATED/RETIRED para deduplicação;
- limita a 48 registros por rodada e não produz porcentagem de acerto a partir
  de scores;
- retorna `WAIT` quando há evidências de pesquisa coerentes ou inconclusivas
  e `DO NOT TRADE` para dados inválidos, conflitantes ou vencidos;
- informa explicitamente `orders_sent=0`,
  `approved_for_trading=false` e `execution_allowed=false`.

**Integrações efetivamente disponíveis:** a I06 reaproveita `strategy_family`
e `research_state` de I02 como metadados de estudo. I04 Knowledge Base e o
Strategy Intelligence específico da arquitetura 2.0 não estão disponíveis
na `main` atual; por isso retornam `NOT_CONNECTED` e não há promessa de
consulta a conhecimento, regras de promoção ou estratégia aprendida.

**O I06 não emite sinais executáveis:** sem `SIGNAL APPROVED`,
`CALL`/`PUT`, expiração contratual ou instante de compra. Os
parâmetros `expiry_minutes` da rota continuam sendo somente o perfil
de análise de timeframes herdado do I03. Resultado de pesquisa não é
autorização para contratos financeiros.

## Especificação da revisão de pesquisa I06

A revisão foi ampliada sobre o motor da PR #59, **sem substituir**
seus registros originais. Cada observação pública possui:

- `research_prerequisites.checklist`: cinco pré-requisitos
  objetivos (candles I01 completos, última janela I02 fechada e atual,
  família de estudo identificada, três timeframes I03 hierarquizados
  e ausência de conflito forte);
- `research_confidence.points`: **0 a 100 em passos de 20**,
  exclusivamente proporção de metadados verificados, nunca confiança
  de acerto, probabilidade de ganho ou força de uma recomendação;
  dados inválidos e vencidos resultam em 0;
- `research_review`: `DATA_READY_FOR_STUDY` ou
  `RECHECK_OR_REJECT`. O status não é sinal de compra;
- `validity_window.from_epoch` e `until_epoch`:
  **validade da observação pública** por 60 segundos, não uma
  janela de entrada em contratos;
- `strategy_provenance`: família e versão da fonte I02,
  com `strategy_version=null` enquanto a interface dedicada não existir;
- `public_context`: tendência observada `UP`, `DOWN` ou
  `NEUTRAL`, jamais convertida em CALL/PUT;
- `knowledge_base`, `strategy_intelligence` e `distribution`:
  adaptadores de estado explícitos `NOT_CONNECTED` ou
  `I02_PUBLIC_STRATEGY_METADATA_ONLY`, sem contratos fictícios
  nem publicação. `publish_allowed=false`.

O motor verifica os horários de **fechamento efetivo** de candles
I01 em todos os três timeframes:
`last_closed_epoch = (observed_at // timeframe_seconds - 1) * timeframe_seconds`.
Candles abertos, futuros, atrasados ou com proveniência divergente
impedem revisão pronta. Mudanças de último candle são incorporadas
ao fingerprint para atualização rastreável.

A rota V4 rejeita resultados forjados mesmo quando os campos de
execução indevida aparecem **dentro** de um item, antes do cache.

### Diferença explícita para o Signal Generation Engine solicitado

Não são emitidos `CALL`, `PUT`, `SIGNAL APPROVED`,
expirações contratuais ou instantes de abertura de posição. Esse
conjunto constituiria recomendações operacionais específicas para
contratos binários. O equivalente seguro entregue é uma triagem de
qualidade de dados e consistência de evidências, **sem instrução
financeira**. A flag I06 permanece OFF; não há envio ao Signal
Distribution Engine.

## Integração

O endpoint autenticado existente `GET /api/research/asset-scanner`
mantém V1/V2/V3 e ganha `version=4` para I06.

Para acessar V4 em um ambiente **de testes** as duas flags precisam estar
explicitamente verdadeiras:

```text
NEXORA_INTELLIGENCE_READONLY_ENABLED=true
NEXORA_INTELLIGENCE_I06_RESEARCH_ENABLED=true
```

Ambas permanecem **desligadas por padrão**. V4 responde HTTP 503 quando
qualquer flag estiver ausente/falsa; anônimos recebem HTTP 401. A consulta
V4 rejeita retorno forjado que autorize operações. Não inclui contratos
particulares nem consulta a tabela de ordens do usuário.

A rota reaproveita o cache público limitado do scanner e adiciona quatro
variantes (perfis I03 de 1, 3, 5, 15 minutos) para V4, limite total de
nove entradas; não inclui estado privado no cache. Deduplicação ao longo
de requisições é somente por processo e **não é armazenamento persistente**
nem garantia de consistência multiprocesso.

## Testes

`tests/test_intelligence2_i06_research_signal.py` cobre qualidade,
contexto, rastreabilidade, vencimento, alterações, duplicações, limite
de capacidade, validação de versão, dupla flag desligada, isolamento
do cache, ausência de consultas privadas e rejeição de saída executável.

CI executa o arquivo isoladamente e depois a suíte geral; as flags
estão `false` no workflow. Nenhuma migração ou conexão de produção
está envolvida.

## Dependência de entrega

A PR #58 foi incorporada à `main`. A branch I06 foi sincronizada
com o merge e a PR #59 aponta para `main`, mantendo diff isolado.
**Não realizar merge, deploy ou alteração de flags** sem aprovação separada.
