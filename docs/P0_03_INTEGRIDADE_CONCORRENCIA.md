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
- **`app/demo_ledger.py`:** mantém retorno da mesma tentativa DEMO quando parâmetros coincidem; recusa conteúdo incompatível com chave já persistida. Configurações e estratégias DEMO não foram alteradas.
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
- CI do commit de implementação e relatório `c8fd987c3d93cd969825c2bcc48bb36af81d85d2`: [GitHub Actions #44619667191](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44619667191), **aprovado**. Suíte geral: **396 aprovados, 8 omitidos, 1 aviso**; teste de carga somente leitura: **1 aprovado**; PostgreSQL descartável: **8 aprovados**; job Chromium (P0-01) omitido porque nenhuma alteração visual foi realizada.
- Esta atualização documental gera um commit posterior, com SHA próprio; verificar seu CI separadamente e não confundir os dois commits.
