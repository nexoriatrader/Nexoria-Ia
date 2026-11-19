# NEXORA IA — P0-05: status, capacidade e transparência operacional

**Escopo exclusivo:** P0-05, desenvolvida em `feature/p0-05-status-capacidade` a partir do SHA **exato** da P0-04 aprovada: `65571b6b25bf46d1a6d570775411b5fa0345256a`. A PR P0-04 #48 permanece aberta e sem merge. Esta entrega não foi implantada.

**Proibições observadas:** sem merge, deploy, migração, alteração de variáveis administrativas REAL, acesso a Supabase de produção, comandos financeiros ou compra na Deriv. Dados de teste são fictícios; as contas e sinais em Chromium são mockados.

## Problema e relação com P0-01 a P0-04

- **P0-01:** DEMO e REAL possuem configurações de conta distintas. O painel antigo usava `configured_account_mode` para selecionar histórico, mas misturava disponibilidade e execução.
- **P0-02:** `PENDING` e `UNKNOWN` devem permanecer pendentes de reconciliação; o histórico não pode inferir resultado.
- **P0-03:** slots e idempotência são persistidos e protegidos entre processos; o status não deve contrariar a integridade dos registros.
- **P0-04:** `running` é um conjunto de memória **local** a cada worker do Render. A revogação de consentimento é persistida, mas nenhum pedido atendido por outro processo pode pressupor que seu conjunto local represente todos os workers. Não existe registro distribuído persistido de vida/heartbeat na base atual.
- **P0-05:** mostrar essas diferenças sem simular disponibilidade, execução, resultado de compra ou liquidação.

## Arquivos alterados

| Arquivo | Modificação de observabilidade |
| --- | --- |
| `app/status_contract.py` **(novo)** | Contrato de status v2 de leitura, estados de contrato, contagens SQL por usuário/conta e avaliação triestado de worker/consentimento |
| `app/main.py` | Campos **aditivos** em `/healthz`, `/api/session`, `/api/autopilot/readiness` e `/api/status`; campos legados preservados |
| `app/demo_ledger.py` | Campo aditivo `contract_stage` no histórico DEMO |
| `app/real_ledger.py` | Campo aditivo `contract_stage` no histórico REAL |
| `app/index.html` | Indicadores claros de desconhecido/desatualizado, detalhamento da capacidade REAL e distinção entre contrato confirmado, aberto e liquidado |
| `tests/test_p005_status_contract.py` **(novo)** | Regressão de contratos, privacidade, sessões, isolamento de contas, compatibilidade, flags independentes, worker local, estado desconhecido e ausência de ordens |
| `tests/test_postgres_p005_status.py` **(novo)** | Teste multiprocesso PostgreSQL descartável: dados persistidos iguais, visibilidade do worker diferente, `CONFIRMED` não liquidado e consentimento revogado |
| `tests/browser_visual_p005.py` **(novo)** | Interface real em Chromium, desktop e celular, APIs fictícias sem rede; campos, painel, controle bloqueado e falha de status |
| `.github/workflows/test.yml` | Suíte original, PostgreSQL local descartável e navegador Chromium habilitados na branch exclusiva P0-05 |

Não há alteração de esquema, estratégia, endpoint financeiro, consentimento, ordem, saldo, API de compra ou comportamento de inicialização dos workers.

## Contrato de status v2 — leitura apenas

A rota autenticada `GET /api/status` acrescenta, sem substituir os campos legados:

- `contract_version: 2`, `configured`, `configured_mode`, `selected_account` e `session_valid`.
- `real_capability`: `configured`, `technical_feature_enabled`, `administrative_release_approved`, `technically_available`, `available_for_execution`, `account_authorized`, `running_now` e `running_evidence`. Cada dimensão é calculada separadamente. A presença de configuração e autorização não habilita compra financeira.
- `demo_capability`: configuração, disponibilidade anterior, consentimento de retomada persistido e execução observável.
- `running_now`: `true` (worker **deste processo** com task de scheduler ativa, autorização persistida e gates permitidos), `false` (bloqueio verificável por gate/ausência de autorização/conta) ou `null` (**desconhecido**, quando não é possível inferir atividade de outros processos).
- `running_state`, `running_evidence`, `state_stale` e `state_scope: LOCAL_PROCESS_PLUS_DATABASE` identificam os limites da observação. `observed_at` é o horário da consulta, **não um heartbeat remoto**.
- `contracts` e `reconciliation_pending`: contagens agregadas do ledger **apenas para o usuário e a conta selecionada**, estado mais recente e necessidade de conferência. Se a conta REAL não estiver selecionada, as contagens são **desconhecidas (null)**; o backend não escolhe outra conta arbitrariamente.

Os estados são informativos e nenhuma consulta aciona operações, muda consentimento, altera estado de um contrato ou deduz resultados financeiros. O volume histórico é consultado com agregações SQL por conta, sem materializar todos os contratos na memória em cada atualização do painel.

## Matriz do ciclo de vida

| Estado do ledger | `contract_stage` | Semântica |
| --- | --- | --- |
| `PENDING` | `RESERVED_UNCONFIRMED` | Reserva pendente, compra não confirmada |
| `UNKNOWN` | `UNKNOWN_UNRESOLVED` | Resultado incerto; revisão ou reconciliação requerida |
| `CONFIRMED` | `CONFIRMED_OPEN_UNSETTLED` | **Compra confirmada, contrato aberto, não liquidado** |
| `SETTLED` | `SETTLED` | Contrato liquidado |
| `CANCELLED` | `CANCELLED` | Reserva cancelada; **não** implica resultado financeiro |
| Estado inesperado | `UNKNOWN_UNVERIFIED` | Estado desconhecido; não exibir como concluído |

Um contrato `CONFIRMED` integra `open_unsettled` e `confirmed_open_count`, jamais `settled_count`. O painel só mostra resultado financeiro após liquidação persistida.

## Compatibilidade e segurança

1. `/api/status` conserva `running`, `auto_buy_enabled`, `real_orders_enabled`, `execution`, `reason`, `scan_progress` e todos os campos existentes. **Atenção:** `running` é um sinal legado **local**, e clientes que necessitam afirmar atividade distribuída devem passar a utilizar `running_now`/sua evidência.
2. `/api/session` mantém `authenticated`, `demo_accounts`, `real_enabled` etc., acrescentando `session_valid` e a versão do contrato. Sessão inválida continua identificada como falsa.
3. `/api/autopilot/readiness` mantém `ready`, `reason`, `access` e modo DEMO, acrescentando diagnósticos informativos de execução e reconciliação sem alterar as regras anteriores de start.
4. `/healthz` permanece público, sem tokens ou identidades de usuários; acrescenta somente versão, escopo local e estados das flags de recurso/aprovação (não segredos).
5. A interface exibe `NÃO CONFIRMADO` se o worker não puder ser atestado em todos os processos. Uma falha de rede exibe `DESATUALIZADO` e desabilita o botão; o último estado local é preservado como cache legado **sem reivindicar atualidade**. Campos de conta/contratos permanecem independentes.

## Critérios de aceite e evidências

- CONFIRMED nunca aparece como liquidado; DEMO e REAL mantêm seus ledgers próprios e a contagem `SETTLED` independente.
- Sem conta REAL escolhida, histórico e contagens permanecem indeterminados, sem mistura de contas ou usuários.
- Sem scheduler local observável, estado global não é inferido; no PostgreSQL, dois processos podem observar a mesma persistência e ter `running_now` distintos conforme a presença de worker local. O segundo informa `null` (desconhecido), e consentimento revogado informa bloqueio. As flags administrativas permanecem **OFF** no ambiente de CI; eventual cenário lógico de gates ligados é apenas entrada de função pura sintética.
- O painel diferencia estado desconhecido, desatualizado e contrato confirmado aberto; testes Node existentes de recuperação de falhas continuam válidos e a interface é exercida em Chromium sem rede externa.
- A suite geral, teste de carga read-only, PostgreSQL descartável e Chromium não chamam Deriv nem liberam qualquer operação financeira.
- Os caminhos de compra `app/real_broker.py` e `app/demo.py` não foram alterados; também não foram tocados `db.py`, schema ou migração.

### Verificações separadas para revisão independente

**Evidências automatizadas não substituem um revisor independente humano.** Antes de aprovação definitiva, verificar especificamente:

1. Que a rota antiga `/api/status.running` permanece compatível e é rotulada como local, sem ser utilizada na UI como comprovação de atividade distribuída.
2. Que um processo sem worker não publica `running_now=false` somente por ausência de flag de memória, quando outro processo pode estar ativo.
3. Que a autorização REAL é consultada no banco e permanece distinta das duas flags administrativas.
4. Que o modo REAL sem conta selecionada não expõe ordens de outra conta.
5. Que a lista de contratos não atribui WIN/LOSS ou P/L a `CONFIRMED` ou `UNKNOWN`.
6. Que os testes Chromium e PostgreSQL usam somente contas fictícias/loopback e que os endpoints públicos não expõem tokens.
7. Que a PR está em rascunho, com base na P0-04, e que a `main` não foi modificada.

### Limitações expressas

- **Não há heartbeat distribuído nem inventário completo de workers.** Um pedido atendido pelo processo B não pode afirmar que o processo A esteja ativo ou parado sem evidência externa. Por isso `running_now=null` é um resultado correto quando faltam evidências. A P0-05 não cria tabelas nem registros de heartbeat para evitar novas migrações; isso poderia ser objeto de análise futura separada.
- Um scheduler ativo e autorizado é **evidência local de worker**, não prova de operação financeira, de conexão Deriv funcional ou de sinal confirmado.
- `state_stale=true` indica liveness **não verificável**, não necessariamente uma leitura de banco antiga; a falha de comunicação com o servidor é tratada distintamente na interface.
- Os relatórios do CI provam apenas execução em ambientes fictícios; não aprovam merge, deploy nem liberam execução financeira.

## Estado final para revisão

- Branch: `feature/p0-05-status-capacidade`
- Base: `65571b6b25bf46d1a6d570775411b5fa0345256a` (P0-04).
- PR da P0-05: **rascunho**, baseada na branch P0-04, sem merge.
- Nenhum deploy, Supabase de produção, ordem Deriv ou mudança de gates.
- Pendências herdadas: confirmar flags administrativas efetivas no Render e inventariar clientes externos que ainda usem APIs legadas separadas.
- **CI de código e de SHA documental:** inserir os links e resultados exatos após a conclusão do GitHub Actions, sem reutilizar evidência de commits anteriores.
