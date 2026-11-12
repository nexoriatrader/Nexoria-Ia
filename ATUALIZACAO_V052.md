# NEXORA IA v0.5.2 — validação Demo supervisionada

## Alterações

O cliente Deriv consulta saldo e moeda oficiais no WebSocket Demo e usa a moeda retornada nas propostas e compras. A validação do URL OTP exige origem WSS oficial, caminho WebSocket demo e um único parâmetro OTP não vazio.

Contratos confirmados são reconciliados em background por consulta read-only; o ledger grava P/L somente após liquidação confirmada pela Deriv. Se a tentativa ficar `UNKNOWN` sem `contract_id`, novas compras permanecem bloqueadas, sem repetição automática. Uma falha ao persistir a confirmação também passa a ser marcada como `UNKNOWN` quando o banco permite.

A interface distingue `LIGAR SIMULAÇÃO` (paper trading local) de `TESTAR COMPRA DEMO` e permite consultar saldo demo. O health check e o OpenAPI reportam a versão `0.5.2`. Compra real e AutoPilot Deriv continuam desabilitados.

## Verificações locais

`pytest -q`: **44 passaram**, com um aviso de depreciação do Starlette/httpx. O JavaScript embutido passou `node --check`; `git diff --check` não reportou problemas. Testes usam mocks e não enviam ordens.

## Estado externo antes da publicação do código

A URL pública respondeu `version: 0.4` e `GET /api/demo/capabilities` retornou 404 na inspeção pré-push. O serviço Render existente acompanha a branch `main` com auto-deploy. Foi configurado `NEXORA_DEMO_BUY_ENABLED=false` por merge, sem ler/substituir outras variáveis; o deploy dessa configuração concluiu `live` ainda sobre o commit anterior `653b57c`. O commit local v0.5.2 ainda precisa ser enviado, o que acionará o deploy de código. Depois, confirme `/healthz` e as rotas antes do login Deriv.

A autenticação Deriv, o saldo oficial, a compra supervisionada, a liquidação e o histórico não foram testados ponta a ponta. Nenhum segredo foi adicionado ao Git. Mantenha a flag desligada até confirmar conta demo autenticada, banco persistente e controles; `UNKNOWN` sem ID exige revisão manual. Não ligar AutoPilot antes do ciclo manual supervisionado.
