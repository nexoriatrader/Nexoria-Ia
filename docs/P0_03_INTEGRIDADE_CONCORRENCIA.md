# NEXORA IA — P0-03: integridade, concorrência e registros consistentes

**Escopo de auditoria:** apenas a P0-03, em `feature/p0-03-integridade-concorrencia`. Branch derivada do commit aprovado da P0-02 `28d36ae5a794d02a923896ccdecc01fd73f6d06f`. A PR #46 continua aberta e sem merge.

**Restrições preservadas:** código de compra, corretora Deriv, OAuth, planos, pagamentos, painel, funcionalidades DEMO e REAL existentes preservados. Nenhum merge, deploy, transação financeira, migração Supabase de produção ou alteração dos gates administrativos foi executado. Os testes são exclusivamente sintéticos e usam bancos descartáveis.

## Diagnóstico da base P0-02

| Achado | Evidência anterior | Correção P0-03 |
| --- | --- | --- |
| Chave idempotente sem equivalência semântica | `reserve_real_order` e `demo_ledger.reserve` retornavam o primeiro registro apenas pela chave, mesmo se ativo/direção/valor diferissem | Reutilização com conteúdo divergente gera erro; replay idêntico continua retornando a ordem existente |
| Escrita de estado baseada em objeto ORM antigo | `confirm_real_order`, `mark_real_unknown`, `cancel_unsubmitted_real_order` e `settle_real_order` alteravam o objeto e executavam commit sem exigir o estado original persistido | Atualizações SQL condicionais com comparação de estado e slot; atualizações atrasadas não sobrescrevem a transição vencedora |
| Integridade de histórico parcialmente diagnosticada | P0-02 já marcava slots retidos/incorretos, mas não sinalizava liquidações sem evidência ou múltiplos registros ativos com slot inconsistente | Auditoria local por usuário/conta, estritamente de leitura, identifica anomalias e impede reserva em histórico contraditório |
| Concorrência entre processos sem regressão direta | Unicidades `real_orders.active_slot`, `uq_real_order_request`, `real_orders.contract_id` e `demo_orders.active_slot` já existiam | Testes de processos independentes em PostgreSQL 16 descartável demonstram um commit e um conflito impedido |
| Falha abrupta no meio de uma transação | Sem teste explícito de perda de processo após editar reserva, antes do commit | Processo sintético encerrado abruptamente; Postgres precisa reverter automaticamente qualquer alteração não confirmada |

## Arquivos alterados

- **Novo `app/order_integrity.py`:** compara semanticamente requests idempotentes e audita somente os registros do usuário/conta selecionados, sem importar `real_broker`, utilizar tokens ou emitir ordens.
- **`app/real_ledger.py`:** proíbe replay divergente, exige auditoria de integridade aprovada e aplica atualizações condicionais às transições de estado. Evita sobrescrever contrato liquidado e preserva slot diante de conflito.
- **`app/demo_ledger.py`:** resolve o stake efetivo a partir da configuração DEMO atual (ou de `stake_override`) **antes** da consulta de idempotência. A função `demo_replay_matches` em `app/order_integrity.py` compara o valor efetivo em todos os casos, inclusive sem override. Replay com mesmo valor continua idempotente; valor divergente é recusado sem alterar o registro antigo. As estratégias DEMO não foram alteradas.
- **`app/real_recovery.py`:** inclui sumário de integridade no resultado existente e trata anomalias como bloqueantes. Nenhum fluxo financeiro novo.
- **Novos `tests/test_p003_integrity.py` e `tests/test_postgres_p003_integrity.py`:** casos de idempotência divergente, duas sessões com objetos antigos, duas liquidações simuladas, crash com rollback, auditorias imutáveis, unicidade entre processos e separação de usuários.
- **`.github/workflows/test.yml`:** CI da branch P0-03, com os testes PostgreSQL adicionados ao serviço descartável de localhost. `NEXORA_REAL_AUTOPILOT_ENABLED=false` e `NEXORA_REAL_RELEASE_APPROVED=false` nesse job.

## Comportamentos com resultados esperados

| Cenário sintético | Resultado objetivo |
| --- | --- |
| Mesma chave e mesmos parâmetros REAL/DEMO | Registro existente, nenhuma duplicação |
| Mesma chave com parâmetros diferentes | Rejeição, registro existente intocado |
| CONFIRMED gravado enquanto sessão antiga prepara UNKNOWN | UNKNOWN atrasado não substitui CONFIRMED |
| CONFIRMED gravado enquanto sessão antiga tenta CANCELLED | Cancelamento atrasado recusado e slot preservado |
| UNKNOWN já confirmado como incerto por varredura P0-02 | Confirmação tardia incompatível não sobrescreve UNKNOWN |
| Dois estados finais concorrentes do mesmo contrato | Apenas o primeiro commit de liquidação é persistido |
| SETTLED sem P&L ou confirmação identificável | Auditoria acusa anomalia e bloqueia nova reserva |
| CONFIRMED sem identificador de contrato | Auditoria exige revisão |
| Mesmo slot em dois processos PostgreSQL | Um registro aceito e um bloqueado pela unicidade |
| Mesma chave idempotente em dois processos PostgreSQL | Um registro aceito e um bloqueado pela unicidade |
| Mesmo identificador de contrato em dois processos PostgreSQL | Um registro aceito e um bloqueado pela unicidade |
| Mesmo slot DEMO em dois processos PostgreSQL | Um registro aceito e um bloqueado pela unicidade |
| Dois processos liquidando o mesmo registro fictício | Um vencedor; segunda atualização não substitui P&L |
| Encerramento abrupto de processo com UPDATE sem COMMIT | Rollback PostgreSQL; reserva original e slot persistem |
| Auditoria repetida e conta de outro usuário | Nenhuma mutação e nenhuma leitura fora do escopo |

A correção é **fail-closed**: qualquer condição inesperada impede alterações conflitantes, mas nunca cria confirmação financeira, liquida contrato sem dados comprovados, libera slot de UNKNOWN, abre canal de compra, nem altera gates de liberação.

## Evidências do banco e limitações

Os testes em PostgreSQL local descartável exercem processos de sistema operacional separados, com conexões SQLAlchemy distintas. Regras de unicidade existentes são verificadas com duas escritas simultâneas; é esperado que o PostgreSQL registre erros de `UNIQUE` na tentativa rejeitada. Isso constitui **sucesso do teste**, não regressão.

Não foram criados índices, tabelas ou migrações nesta etapa: as verificações de integridade são read-only ou utilizam transações nas tabelas existentes. Os campos `recovery.integrity` adicionados ao histórico REAL são informativos, **nunca** autorização para novas transações.

O encerramento abrupto é simulado apenas em processo descartável do CI; o processo e seu banco não pertencem à produção. Em caso de dados legados inconsistentes, o sistema evita escrita perigosa e exige revisão; **não apaga registros nem gera comprovantes**.

## Pendências pré-implantação preservadas

1. Confirmar no workspace Render autorizado que `NEXORA_REAL_AUTOPILOT_ENABLED` e `NEXORA_REAL_RELEASE_APPROVED` permanecem desligados no ambiente. Esta P0-03 não lê nem modifica o workspace Render.
2. Identificar integrações externas que ainda chamam separadamente `POST /api/automation/profile` e `POST /api/onboarding/config`, em vez do endpoint atômico criado na P0-01.

Essas pendências continuam registradas na PR #45 e não impedem revisão técnica isolada, mas são obrigatórias antes de eventual implantação.

## Estado de revisão

- Branch: `feature/p0-03-integridade-concorrencia`
- PR P0-03: [#47](https://github.com/nexoriatrader/Nexoria-Ia/pull/47), **aberta em rascunho**, baseada na branch P0-02 (PR #46), nunca em `main`.
- P0-01 e P0-02: não mescladas, sem deploy.
- P0-04 e seguintes: **não iniciadas**.
- CI verificado dos dois ajustes solicitados na revisão independente, commit de código/testes `943ef34c6fe58bb7183d4129be8a918121b96d62`: [GitHub Actions #44622312604](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44622312604), **aprovado**. Suíte geral: **397 aprovados, 10 omitidos, 1 aviso**; carga somente leitura: **1 aprovado**; PostgreSQL descartável: **10 aprovados**; Chromium da P0-01 omitido (interface sem alterações).
- Este relatório gera um commit exclusivamente documental com SHA próprio. A referência acima identifica a execução exata do código/testes revisados, sem confundir o commit documental com o de implementação.

## Ajustes de revisão independente — validação final

### Divergência do stake padrão DEMO corrigida

**Problema confirmado:** o `demo_replay_matches` antigo tratava `stake_override=None` como dispensa da comparação de stake. A mesma chave podia retornar um registro com valor antigo após alteração do stake padrão.

**Correção:** `demo_ledger.reserve` lê o stake efetivo (`locked_user.stake` sem override, ou o override explícito) **antes** da busca pela chave de requisição, e o passa obrigatoriamente para `demo_replay_matches`. A comparação usa `Decimal` com rejeição de valores inválidos; não há tolerância para valor divergente.

**Regressão demonstrada:** `test_demo_default_stake_change_rejects_replayed_key_without_override` em `tests/test_p003_integrity.py` cria DEMO sintética com 1,00 USD, altera o valor padrão para 2,00 USD, tenta a mesma chave sem override e verifica a rejeição; confere todos os campos fundamentais do registro original e quantidade de registros igual a um. Um override explícito de 1,0 continua reconhecido como replay do mesmo valor, preservando compatibilidade.

### Concorrência da função da aplicação em processos independentes

**Novo ensaio:** `test_cross_process_application_real_reservation_never_duplicates` em `tests/test_postgres_p003_integrity.py` inicia dois processos de sistema operacional separados, com conexões SQLAlchemy independentes e início simultâneo coordenado. Cada processo chama **a própria função existente** `reserve_real_order` contra PostgreSQL 16 descartável, com usuário, consentimento e configuração de conta **inteiramente fictícios**, sem token de corretora, API ou `real_broker`.

Foram testados dois casos: requisições com chaves **diferentes** para a mesma conta (uma única reserva persistida e a tentativa concorrente recusada), e requisições com **a mesma chave** (uma única reserva persistida e a segunda requisição reconhecida como idempotente ou bloqueada conservadoramente em caso de corrida). O banco é consultado após os processos terminarem para verificar uma só linha, `PENDING` com slot preservado e ausência de identificador de contrato, P/L ou segunda reserva.

**Correção de isolamento da suíte:** a primeira execução do novo teste reutilizou um identificador de conta fictícia entre os dois cenários, fazendo o segundo encontrar o slot ativo deixado pelo primeiro. O ledger recusou corretamente ambas as tentativas. As fixtures foram separadas por cenário; o CI final subsequente aprovou ambos, sem mudanças na lógica REAL.

### Evidências e aceitação

- Código e testes: commit `943ef34c6fe58bb7183d4129be8a918121b96d62`.
- [CI #44622312604](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44622312604): **397 aprovados, 10 omitidos, 1 aviso** na suíte geral; **10 aprovados** no PostgreSQL descartável, incluindo as duas execuções de `reserve_real_order`; **1 teste de carga somente leitura aprovado**.
- Confirmação externa: **nenhuma**. Os testes não executam ordens financeiras nem demonstram autorização para operar. As flags administrativas continuam desativadas no CI.
- A PR #47 permanece aberta e em rascunho, sem merge, deploy ou migração Supabase; P0-04 não foi iniciada.
