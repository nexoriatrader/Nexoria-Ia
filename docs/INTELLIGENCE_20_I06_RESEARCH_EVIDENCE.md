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
- limita a 48 registros por rodada e não produz porcentagem de acerto a partir
  de scores;
- retorna `WAIT` quando há evidências de pesquisa coerentes ou inconclusivas
  e `DO NOT TRADE` para dados inválidos, conflitantes ou vencidos;
- informa explicitamente `orders_sent=0`,
  `approved_for_trading=false` e `execution_allowed=false`.

**O I06 não emite sinais executáveis:** sem `SIGNAL APPROVED`,
`CALL`/`PUT`, expiração contratual ou instante de compra. Os
parâmetros `expiry_minutes` da rota continuam sendo somente o perfil
de análise de timeframes herdado do I03. Resultado de pesquisa não é
autorização para contratos financeiros.

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

A branch I06 parte da correção de CI da PR #58. A PR da I06 deve ter
como base a branch dessa correção até a PR #58 ser incorporada; assim
o diff I06 contém apenas a sua implementação. **Não realizar merge,
deploy ou alteração de flags** sem aprovação separada.
