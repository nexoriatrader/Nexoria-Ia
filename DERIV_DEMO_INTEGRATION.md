# Integração Deriv Demo — v0.5.2

## Escopo

A integração autentica somente uma conta identificada como demo/virtual. Ela consulta saldo e moeda oficiais, faz proposal read-only e permite uma compra manual na conta demonstrativa quando `NEXORA_DEMO_BUY_ENABLED=true`. Nenhum caminho de compra real existe. O botão `LIGAR SIMULAÇÃO` é paper trading local; AutoPilot Deriv Demo permanece desabilitado.

## Fluxos implementados

1. O usuário entra por OAuth 2.0 + PKCE. A aplicação não cria sessão se não identificar explicitamente uma conta demo.
2. O servidor obtém OTP para a conta associada à sessão e aceita apenas `wss://api.derivws.com/trading/v1/options/ws/demo` com um único parâmetro OTP não vazio.
3. Pelo canal demo, o backend consulta saldo/moeda; proposal usa a moeda retornada pelo saldo oficial.
4. A compra requer modo demo, flag habilitada, confirmação da interface e uma reserva idempotente persistida antes da chamada à Deriv.
5. O ledger permite no máximo um contrato pendente/ativo por conta. P/L é gravado apenas depois que a Deriv informa contrato vendido/liquidado e lucro/prejuízo finito.
6. Um reconciliador em background consulta contratos já confirmados. A rotina é somente leitura e nunca compra.
7. Ordem em estado `UNKNOWN` não é repetida. Se não houver `contract_id`, a aplicação não consegue identificar com segurança qual contrato da conta corresponde à tentativa; o slot fica bloqueado para revisão manual com a Deriv.

## Rotas internas

- `GET /api/demo/capabilities`: estado da flag e indicação explícita de que compras reais e AutoPilot estão desabilitados.
- `GET /api/demo/balance`: consulta read-only de saldo/moeda no canal autenticado demo.
- `POST /api/demo/proposal`: proposta oficial, sem compra.
- `POST /api/demo/buy`: tentativa manual demo com stake configurado, chave idempotente e reserva antes da chamada externa.
- `GET /api/demo/orders`: ledger de pedidos oficiais demo.
- `POST /api/demo/orders/{id}/reconcile`: consulta status/liquidação de contrato confirmado.
- `GET /api/demo/contract/{contract_id}`: status read-only de contrato pertencente à conta autenticada.

A confirmação por `window.confirm()` é uma barreira de interface. O marcador do endpoint não prova, por si só, que uma pessoa clicou no diálogo; portanto, proteja a sessão autenticada e não exponha o serviço a usuários externos antes de uma revisão de CSRF/autorização.

## Configuração

- `NEXORA_DEMO_BUY_ENABLED` permanece `false` por padrão.
- Defina `true` apenas no ambiente de validação depois de conferir conta Demo autenticada, PostgreSQL persistente, sessão criptografada e limites.
- Martingale está bloqueado para compras demo.
- Nunca configure compra real. O endpoint OTP e o cliente WebSocket validam que o canal retornado é o demo.

## Validação automatizada e pendências

A suíte local usa mocks; não prova conectividade Deriv. O teste supervisionado ainda precisa demonstrar, na conta demo autenticada, saldo, proposal, confirmação manual, buy, contrato confirmado, liquidação e P/L oficial no painel/histórico. AutoPilot não deve ser ligado antes desse ciclo.

Casos `UNKNOWN` sem `contract_id` ficam fail-closed e precisam de procedimento manual; não existe recuperação automatizada segura para eles nesta versão. A URL Render pública consultada ainda respondia v0.4 e não expunha as rotas novas; publique e verifique o artefato v0.5.2 antes do teste demo.

## Documentação oficial

- [OAuth 2.0](https://developers.deriv.com/docs/intro/oauth/)
- [Options Setup e endpoints de contas/OTP](https://developers.deriv.com/docs/options/)
- [WebSocket Demo](https://developers.deriv.com/docs/options/ws-demo/)
- [WebSockets/OTP](https://developers.deriv.com/docs/options/websocket/)
- [Workflow completo](https://developers.deriv.com/docs/workflows/)
- [Proposal](https://developers.deriv.com/docs/trading/proposal/)
- [Buy](https://developers.deriv.com/docs/trading/buy/)
- [Open Contract Status](https://developers.deriv.com/docs/trading/proposal-open-contract/)
