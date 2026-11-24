# NEXORA Intelligence 2.0 — Qualidade de OHLC público no painel principal

## Escopo implementado

A **aplicação existente** (`app.main:app`, branch da PR #66) agora possui a rota autenticada `GET /api/intelligence20/market/quality` e o botão **VERIFICAR VELAS DA DERIV** na primeira etapa de configuração guiada.

Não é um aplicativo independente: a interface e a rota utilizam o painel e a autenticação existentes. As solicitações continuam exclusivamente de **pesquisa pública, descritiva e somente leitura**.

### Fluxo de dados

1. O usuário autenticado informa o código do ativo e escolhe entre M1, M5, M15 ou H1.
2. A API valida os parâmetros e chama `app.intelligence2.public_candle_quality.inspect_public_candles`.
3. O adaptador reutiliza `app.intelligence2.market_data.read_closed_window`, que por sua vez consulta `app.market.shared_candle_request` — o caminho público já existente da Deriv, com cache compartilhado, deduplicação e limite de solicitações.
4. Solicita uma vela adicional para admitir que a vela mais recente esteja em formação. Só considera o conjunto **COMPLETE** se 60 velas fechadas foram realmente recebidas, sem lacunas nem atraso no último intervalo.
5. Os dados são limitados a contagem, carimbos de hora UTC, último fechamento, mínimo/máximo de fechamento, tipo de qualidade e SHA-256 calculado dos dados **recebidos**. Não existe projeção ou previsão.
6. Se o feed estiver fora do ar, o servidor retorna HTTP 503 e o painel exibe **Dados indisponíveis**, sem preencher lacunas ou apresentar valores inventados.

### Limites de autenticidade

`source_claim` descreve o caminho público de coleta do sistema. `source_independently_certified=false`: a validação sintática e de completude não prova de forma criptográfica que a corretora publicou cada preço, especialmente em respostas reaproveitadas do cache público. No teste CI, a fonte é **mockada** e **não demonstra disponibilidade externa real**. Nenhum mock é mostrado em produção como cotação atual.

A página é somente de pesquisa histórica, sem sinais, recomendações ou acionamento de compras. Nenhum parâmetro de automação foi aplicado, nenhum contrato DEMO ou REAL foi executado e o endpoint não acessa credenciais financeiras.

### Situação de publicação

A mudança existe **somente na branch da PR #66**, ainda em draft. O serviço Render `nexora-intelligence20-homologation-demo` executa um processo ASGI **diferente**, com sensores sintéticos; seu deploy bem-sucedido não implica que a nova tela esteja publicada no site principal. Para visualizar a implementação no aplicativo hospedado, é necessária uma publicação futura do painel principal após validação segura de migração e do isolamento financeiro.

### Testes

`tests/test_nexora20_public_market_quality.py` cobre autenticação, validação de query, integração até o leitor público existente, vela em formação, janelas atrasadas, falta de velas, interrupção de feed, estatísticas somente de conjuntos completos e ausência de ordens.
