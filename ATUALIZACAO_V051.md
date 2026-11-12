# NEXORA IA v0.5.1 — Testes na conta Deriv DEMO

## Mudanças
- Botão visível `TESTAR COMPRA DEMO` no painel (com confirmação para cada ordem de teste).
- Endpoint `/api/demo/capabilities` para saber se a compra supervisionada está habilitada.
- Prompt principal `PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md` como fonte de prioridade para testes na conta DEMO.
- Instruções anteriores atualizadas com ponte para o prompt principal.
- Testes adicionais de interface e do prompt.

## Configuração pendente no ambiente
Definir `NEXORA_DEMO_BUY_ENABLED=true` apenas após assegurar sessão Deriv Demo, banco persistente e limites de risco adequados. Esta flag NÃO libera conta real.

## O que NÃO foi validado
- Autenticação real na Deriv, consulta de saldo e ordem demo ponta a ponta.
- Liquidação oficial com a Deriv e reconciliação de ordens de estado UNKNOWN.
- AutoPilot executando compras em Deriv Demo automaticamente.
- Implantação no GitHub/Render/Supabase: este ZIP não altera esses serviços.

## Testes
`pytest -q`: 28 testes locais passaram no ambiente do pacote; não são prova de integração externa.
