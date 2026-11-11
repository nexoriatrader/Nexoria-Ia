# Integração Deriv Demo — Fase 2

## Escopo desta fase

A aplicação obtém o canal autenticado da conta demonstrativa por OTP e consulta uma `proposal` oficial. Esta fase não envia `buy`, `sell` nem qualquer outra instrução que altere uma posição.

O cliente rejeita qualquer OTP que não contenha explicitamente `/trading/v1/options/ws/demo`. URLs do canal real são bloqueadas antes da abertura do WebSocket.

## Fluxo implementado

1. O usuário autentica por OAuth no NEXORA IA.
2. O servidor recupera o token OAuth criptografado da sessão e a conta demo identificada.
3. O servidor chama `POST /trading/v1/options/accounts/{account_id}/otp`.
4. O servidor valida que o OTP aponta para o endpoint WebSocket demo.
5. O servidor consulta `proposal` no WebSocket autenticado.
6. A resposta informa `demo_only: true` e `bought: false`.

## Endpoint interno

`POST /api/demo/proposal` exige sessão autenticada e aceita `symbol`, `direction` (`CALL` ou `PUT`) e `minutes` (`1`, `3` ou `5`). O stake é obtido da configuração do usuário, não de um valor arbitrário do navegador.

## Segurança

O módulo não contém endpoint de compra. Os testes verificam que a mensagem enviada contém `proposal: 1` e não contém a chave `buy`. O modo real continua bloqueado pela aplicação.

## Referências oficiais

- [Deriv Workflows](https://developers.deriv.com/docs/workflows/): OTP, WebSocket autenticado demo e fluxo proposal/buy/open contract.
- [WebSocket Demo Endpoint](https://developers.deriv.com/docs/options/ws-demo/): endpoint `/trading/v1/options/ws/demo` e autenticação por OTP.
- [Price Proposal](https://developers.deriv.com/docs/trading/proposal/): parâmetros oficiais de `proposal`.
- [Buy Contract](https://developers.deriv.com/docs/trading/buy/): reservado para uma fase posterior com confirmação explícita e controles adicionais.
- [Open Contract Status](https://developers.deriv.com/docs/trading/proposal-open-contract/): acompanhamento oficial de contratos, ainda não ativado nesta fase.
