# Integração Deriv Demo — Fase 2

## Escopo desta fase

A aplicação obtém o canal autenticado da conta demonstrativa por OTP, consulta uma `proposal` oficial e já possui compra demo protegida por confirmação explícita. Nenhuma operação real é aceita.

O cliente rejeita qualquer OTP que não contenha explicitamente `/trading/v1/options/ws/demo`. URLs do canal real são bloqueadas antes da abertura do WebSocket.

## Fluxo implementado

1. O usuário autentica por OAuth no NEXORA IA.
2. O servidor recupera o token OAuth criptografado da sessão e a conta demo identificada.
3. O servidor chama `POST /trading/v1/options/accounts/{account_id}/otp`.
4. O servidor valida que o OTP aponta para o endpoint WebSocket demo.
5. O servidor consulta `proposal` no WebSocket autenticado.
6. A consulta informa `demo_only: true` e `bought: false`.
7. A compra demo exige o marcador explícito `DEMO_ONLY` e retorna o identificador do contrato.
8. O status do contrato é consultado por `proposal_open_contract`, sem alterar a posição.

## Endpoint interno

`POST /api/demo/proposal` exige sessão autenticada e aceita `symbol`, `direction` (`CALL` ou `PUT`) e `minutes` (`1`, `3` ou `5`). O stake é obtido da configuração do usuário, não de um valor arbitrário do navegador.

`POST /api/demo/buy` exige a confirmação literal `DEMO_ONLY` e só pode abrir contrato no WebSocket `/ws/demo`. `GET /api/demo/contract/{contract_id}` consulta o status mais recente do contrato demo.

## Segurança

O módulo rejeita qualquer canal que não seja demo. Os testes verificam a confirmação obrigatória, a mensagem `proposal: 1`, a compra somente no canal demo e a ausência de execução quando a confirmação não é fornecida. Não existe endpoint equivalente para conta real; o modo real continua bloqueado pela aplicação.

## Referências oficiais

- [Deriv Workflows](https://developers.deriv.com/docs/workflows/): OTP, WebSocket autenticado demo e fluxo proposal/buy/open contract.
- [WebSocket Demo Endpoint](https://developers.deriv.com/docs/options/ws-demo/): endpoint `/trading/v1/options/ws/demo` e autenticação por OTP.
- [Price Proposal](https://developers.deriv.com/docs/trading/proposal/): parâmetros oficiais de `proposal`.
- [Buy Contract](https://developers.deriv.com/docs/trading/buy/): parâmetros oficiais usados no fluxo demo protegido.
- [Open Contract Status](https://developers.deriv.com/docs/trading/proposal-open-contract/): acompanhamento oficial do contrato demo.
