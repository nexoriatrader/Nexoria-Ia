# NEXORA — estabilidade do feed público Deriv

Registro técnico de estabilidade do feed público. Os testes locais usam mocks e não substituem a verificação de conectividade contínua nos logs do Render.

## Diagnóstico
Logs do Render (antes da correção) mostraram episódios recorrentes de
`Public feed unavailable: ConnectionClosedError`, frequentemente com
intervalos próximos de dois minutos. O feed legada tinha ping/pong WebSocket
de protocolo, mas não emitia heartbeat de aplicativo. A Deriv recomenda ping
a cada 30–60 segundos e reconexão com backoff.

Referências:
- https://developers.deriv.com/docs/system/ping/
- https://developers.deriv.com/docs/best-practices/
- https://developers.deriv.com/docs/options/websocket/

## Correção
- `app/market.py`: `public_feed_messages` faz ping público
  `{"ping":1,"req_id":7301}` a cada 35 segundos (somente leitura).
- O mesmo leitor processa respostas, evitando `recv()` concorrentes.
- `app/main.py`: a conexão só passa a ONLINE após cotação de fato recebida.
- Ao cair, limpa o último tick e utiliza reconexão progressiva já existente.
- Sem ordens nem sinais inventados para simular funcionamento.
- Testes de silêncio, heartbeat, primeiro tick e desconexão:
  `tests/test_public_feed_keepalive.py`.

## Homologação após deploy da PR
1. Conferir /healthz, /api/session e login Deriv DEMO.
2. Analisar logs por 60–120 minutos. Não deve haver reconexões periódicas
   coincidentes com inatividade nem aumento de conexão por scanner.
3. Confirmar atualização de ticks reais e scanner mostrando ativos consultados.
4. Induzir queda controlada em staging com mock e confirmar recuperação.
5. Validar dados do scanner realmente retornados pela Deriv, sem marcação
   fictícia de online nem compra durante indisponibilidade.
6. Reavaliar a margem de CPU/memória do compute ativo e as cotas de API sob carga observada.
7. Se falhas persistirem, coletar código/causa de fechamento de WebSocket e
   validar bloqueios de rede, limites da Deriv e payload de subscrição.

Os testes atuais usam mocks e não estabelecem, por si só, conectividade
duradoura em produção.
