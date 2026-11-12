# NEXORA IA v0.5.2 — validação Demo supervisionada

## Alterações

O cliente Deriv agora consulta saldo e moeda oficiais no WebSocket Demo e usa a moeda retornada nas propostas e compras. A validação do URL OTP exige origem WSS oficial, caminho WebSocket demo e um único parâmetro OTP não vazio.

Contratos confirmados são reconciliados em background por consulta read-only; o ledger grava P/L somente após liquidação confirmada pela Deriv. Se a tentativa ficar `UNKNOWN` sem `contract_id`, novas compras permanecem bloqueadas, sem repetição automática. Uma falha ao persistir a confirmação também passa a ser marcada como `UNKNOWN` quando o banco permite.

A interface distingue `LIGAR SIMULAÇÃO` (paper trading local) de `TESTAR COMPRA DEMO` e permite consultar saldo demo. O health check e o OpenAPI reportam a versão `0.5.2`. Compra real e AutoPilot Deriv continuam desabilitados.

## Verificações locais

`pytest -q`: **44 passaram**, com um aviso de depreciação do Starlette/httpx. O JavaScript embutido passou `node --check`; `git diff --check` não reportou problemas. Testes usam mocks e não enviam ordens.

## Pendências externas

A autenticação Deriv, o saldo oficial, a compra supervisionada, a liquidação e o histórico ainda não foram testados ponta a ponta. A URL pública verificada continua na versão 0.4 e não expõe as rotas novas; v0.5.2 ainda precisa ser publicada. Nenhuma variável secreta foi configurada ou adicionada ao Git.

O flag `NEXORA_DEMO_BUY_ENABLED` permanece `false` no exemplo e desligado por padrão. Habilite-o apenas no ambiente de validação, após confirmar conta demo autenticada e PostgreSQL persistente. `UNKNOWN` sem ID continua exigindo revisão manual. Não ligar AutoPilot antes de completar o ciclo manual supervisionado.
