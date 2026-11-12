# Validação da API Deriv — 12 nov 2026

A documentação oficial atual foi confrontada com o cliente NEXORA. O fluxo documentado usa OAuth 2.0/PKCE, chama o endpoint REST OTP para a conta escolhida e então abre o URL WebSocket retornado; a Deriv informa que o OTP vale 120 segundos e é de uso único. A URL demo é `/trading/v1/options/ws/demo`, separada do endpoint real. [1] [2] [3]

No WebSocket autenticado, a documentação de workflows exemplifica a leitura de saldo com `balance: 1` e `req_id`. A proposta usa `amount`, `basis`, `contract_type`, `currency`, `duration`, `duration_unit` e `underlying_symbol`; a moeda deve ser a da conta. A compra usa o ID da proposta e `price` como preço máximo. Acompanhar o contrato requer `proposal_open_contract` e seu `contract_id`. [3] [4] [5] [6]

O cliente v0.5.2 consulta saldo e moeda antes de proposal/compra e valida o OTP para aceitar somente o host/path demo e um parâmetro OTP único. A liquidação automática consulta apenas ordens já confirmadas; não envia compras. Uma tentativa `UNKNOWN` sem ID não é atribuída a um contrato por heurística: continua bloqueada para revisão manual, evitando compra duplicada ou P/L presumido.

A leitura da documentação não equivale a uma integração ponta a ponta. Nenhum token foi usado e nenhuma ordem foi enviada nesta revisão. A URL pública indicada ainda respondia `version: 0.4` e `GET /api/demo/capabilities` retornou 404; publique a versão atualizada antes de usar a conta demo.

## Referências

[1]: https://developers.deriv.com/docs/intro/oauth/ "Deriv OAuth 2.0"
[2]: https://developers.deriv.com/docs/options/ws-demo/ "Deriv WebSocket Demo Endpoint"
[3]: https://developers.deriv.com/docs/workflows/ "Deriv Complete Workflows"
[4]: https://developers.deriv.com/docs/trading/proposal/ "Deriv Price Proposal"
[5]: https://developers.deriv.com/docs/trading/buy/ "Deriv Buy Contract"
[6]: https://developers.deriv.com/docs/trading/proposal-open-contract/ "Deriv Open Contract Status"
