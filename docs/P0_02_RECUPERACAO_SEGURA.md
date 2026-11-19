# NEXORA IA — P0-02 — Recuperação conservadora de PENDING e UNKNOWN

**Escopo:** desenvolvimento em branch própria `feature/p0-02-safe-unresolved-recovery`, baseado no commit aprovado da P0-01 `3015ab0fc349ea0db1e957f96612e0b7ee4d4b9c`.

**Restrições obedecidas:** nenhum merge, deploy, migração Supabase, alteração de configuração REAL de liberação, compra, reenvio de ordem ou conexão financeira real realizada por este trabalho. Regressões trabalham com dados fictícios e mocks.

## Diagnóstico na origem

- `app/real_runtime.py::reconcile_real_once()` considerava somente `CONFIRMED` ou `UNKNOWN` com `contract_id`; reservas `PENDING` antigas não eram inspecionadas pelo recuperador.
- O fluxo de compra já registrava `PENDING` antes de I/O e `UNKNOWN` quando o envio poderia ter sido recebido mas não confirmado.
- `UNKNOWN` sem ID do contrato não oferece base segura para consulta contratual específica. É incorreto concluir WIN/LOSS, cancelar ou reenviar a compra apenas porque o relógio avançou.
- O bloqueio de duplicidade para uma conta já se fundamentava em `real_orders.active_slot` exclusivo, na chave idempotente `(account_id,request_key)` e na avaliação de qualquer registro ativo.
- Um registro `PENDING` antigo podia permanecer sem diagnóstico de recuperação claramente visível no histórico, embora o bloqueio continuasse aplicável.

## Implementação estritamente não financeira

1. **`app/real_recovery.py` (novo):** classificador local de registros, diagnóstico de integridade, sumário por usuário/conta e varredura de reservas antigas. Não importa `real_broker`, não consulta credenciais, não transmite ordens e não liquida contratos.
2. **`promote_stale_pending_to_unknown`:** após 15 minutos desde a última atualização, se o registro continuar `PENDING` e seu `active_slot` corresponder à conta, uma atualização condicional no banco altera somente `state`, `error_summary` e `updated_at`. **O slot permanece reservado**, o `request_key`, `account_id`, `contract_id`, `stake` e `pnl` não mudam. O processo trabalha em lotes limitados (até 100), é idempotente e faz rollback diante de falhas.
3. **`app/real_runtime.py`:** chama a varredura **local** no início da reconciliação existente. A consulta oficial de contratos com ID permanece no fluxo previamente existente, sem qualquer alteração na implementação de compra. Não há reenvio nem conclusão baseada somente em prazo.
4. **`app/real_ledger.py`:** acrescenta `recovery` ao resultado de `public_real_order` para explicitar situações que exigem revisão, preservando os campos antigos.
5. **`app/main.py::real_orders_route`:** acrescenta o sumário `recovery` à resposta **de leitura** de `GET /api/real/orders`, sempre filtrado por usuário e conta. O histórico DEMO continua independente e inalterado.
6. **`.github/workflows/test.yml`:** inclui a nova branch na suíte preexistente, com DEMO desabilitado para compras nos testes e REAL com dois bloqueios administrativos inalterados por padrão.

## Matriz de estados e comportamentos permitidos

| Entrada | Saída local | Slot | Contrato/P&L | Reenvio |
| --- | --- | --- | --- | --- |
| PENDING recente (menos de 15 min) | PENDING | Preservado | Inalterados | Nunca |
| PENDING antigo com slot íntegro | UNKNOWN | Preservado | Inalterados | Nunca |
| PENDING com slot ausente/incorreto | PENDING + INTEGRITY_REVIEW | Não fabricado | Inalterados | Nunca |
| UNKNOWN sem contract_id | MANUAL_REVIEW_NO_CONTRACT | Preservado | Nenhum resultado presumido | Nunca |
| UNKNOWN com contract_id | AWAITING_OFFICIAL_SETTLEMENT | Preservado até comprovação | Apenas resposta oficial existente pode confirmar | Nunca |
| CONFIRMED com contract_id | AWAITING_OFFICIAL_SETTLEMENT | Preservado | Apenas resposta oficial existente pode confirmar | Nunca |
| CONFIRMED sem contract_id | INTEGRITY_REVIEW | Preservado | Inalterados | Nunca |
| SETTLED ou CANCELLED | CLOSED | Como registrado | Não reescrito | Nunca |

**Critério de segurança:** `UNKNOWN` sem ID não deve ser liberado automaticamente, nem por timeout, nem por ausência de sessão, nem por repetição do reconciliador. Exige evidência independente e processo de revisão em etapa autorizada. Não foi criada uma ferramenta para enviar novas ordens reais ou habilitar seu envio.

## Verificações automatizadas

Arquivo novo `tests/test_real_recovery.py` testa, exclusivamente com dados sintéticos:

- Resgate de PENDING antigo para UNKNOWN sem soltar o slot;
- PENDING recente ainda ativo;
- Repetição idempotente e preservação de chave de requisição;
- Contrato conhecido, sem inventar liquidação;
- UNKNOWN sem ID com retenção mesmo após longo período;
- Proteção contra duplicidade com nova chave e consulta idempotente da chave antiga;
- Tratamento conservador de slot inconsistente;
- Preservação de registros SETTLED/CANCELLED;
- Sumários segregados por usuário e conta;
- Nenhuma chamada ao broker para UNKNOWN sem ID;
- Contrato conhecido reconciliado por **resposta oficial simulada**, apenas uma vez;
- Contrato não vendido continua ativo e P&L ausente;
- Resposta com ID divergente não encerra o contrato;
- Reabertura de sessão / reinício simulado mantém o hold;
- Parâmetros inválidos rejeitados;
- GET de histórico autenticado e sem vazamento de registros;
- Erro da consulta oficial simulada não solta o slot;
- Preservação integral de DEMO;
- Lotes limitados sem perda de locks.

### Conclusão e limites de escopo

Esta etapa **melhora a identificação, a persistência, o estado após interrupções e a impossibilidade de duplicações**. Não pretende inferir se uma compra incerta ocorreu: se a corretora não forneceu `contract_id`, uma confirmação externa/manual ainda é necessária e a conta permanece bloqueada. Isso é um resultado técnico intencional de segurança, não uma tentativa de simular certeza.

A P0-03 (integridade/concor­rência abrangente) e P0-04 (restauração de estado de serviço) **não foram iniciadas**; as proteções existentes foram mantidas. O sistema REAL não foi liberado.

## Pendências pré-implantação herdadas e expressamente preservadas da P0-01

1. **Render:** confirmar em workspace autorizado os valores efetivos de `NEXORA_REAL_AUTOPILOT_ENABLED` e `NEXORA_REAL_RELEASE_APPROVED`. A validação de código mostra operação lógica `AND` e defaults `false`, mas **não substitui a inspeção das variáveis reais do Render**. Não foi consultado/alterado nenhum workspace Render.
2. **APIs antigas:** identificar consumidores externos que ainda salvam perfil e limites em requisições separadas `POST /api/automation/profile` e `POST /api/onboarding/config`. O configurador principal usa `POST /api/automation/configuration` atômico desde P0-01, e APIs antigas seguem compatíveis. A enumeração de consumidores externos requer análise de acessos/documentação externa, não confirmada nesta auditoria.

Nenhuma dessas pendências é autorização para merge/deploy.

## Estado de entrega

- Branch: `feature/p0-02-safe-unresolved-recovery`
- Referência de P0-01 aprovada: `3015ab0fc349ea0db1e957f96612e0b7ee4d4b9c`
- PR P0-01 #45: permanece sem merge, não alterar.
- PR P0-02: somente revisão em rascunho, contra branch P0-01, nunca `main`.
- CI de integração sintética [GitHub Actions #44616358791](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44616358791): **378 testes aprovados, 2 omitidos, 1 aviso**, mais **1 teste independente de carga somente leitura aprovado**, além de **2 testes PostgreSQL 16 descartável aprovados**. Os testes PostgreSQL são executados em job próprio; o job Chromium P0-01 é propositalmente omitido nesta branch sem alterações de interface.
- Resultado de CI para o commit final: consultar a última execução da branch; logs anteriores não comprovam alterações posteriores.

### Casos de apresentação segura reforçados após a primeira validação

- Nenhuma conta selecionada: o resumo devolve `blocked: null` (situação **não verificada**), nunca um falso `blocked: false`.
- Registro `SETTLED`/`CANCELLED` que ainda retém `active_slot`: classificado `INTEGRITY_REVIEW`, também considerado bloqueante no sumário da conta, sem alterar o registro ou liberar o slot.
- Sumário de `GET /api/real/orders` autenticado é restrito ao usuário e à conta selecionada; os testes incluem conta de outro usuário e sessão anônima.
- Os diagnósticos não substituem a confirmação oficial nem geram qualquer autorização de compra.
