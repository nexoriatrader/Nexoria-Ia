# NEXORA IA — P0-04: Consistência após reinicialização do serviço

**Base aprovada:** PR #47, commit `43b22bd57f0a330198cb4498bc2d82f12caaa0fb`. **Branch de trabalho:** `feature/p0-04-consistencia-reinicio`. **Escopo exclusivo:** P0-04. P0-05 não iniciada.

**Regras de segurança:** sem merge, deploy, migração no Supabase de produção, ordem financeira, ativação REAL ou alteração dos gates administrativos. Testes usam SQLite isolado, PostgreSQL 16 descartável de localhost, respostas sintéticas e funções de mercado substituídas por mocks.

## Objetivo e problema encontrado

A NEXORA guarda em memória os conjuntos de execução `main.autopilot_enabled` (DEMO) e `real_runtime.running` (REAL), enquanto os consentimentos, limites e ordens são persistidos no banco. Antes da P0-04, o processo já previa retomada DEMO apenas com `AutoPilotResumeConsent` e verificações de segurança, mas faltava uma preparação de reinício centralizada que limpasse caches temporários e invalidasse consentimentos REAL anteriores a uma falha. O perfil de automação REAL também precisava ser considerado antes de restaurar um consentimento legado DEMO.

Há ainda um problema de ciclo de vida: a tarefa `market_selfcheck_task` era iniciada dentro do `lifespan` sem estar declarada `global`, podendo escapar do cancelamento no encerramento.

## Mudanças de código

| Arquivo | Modificação | Garantia |
| --- | --- | --- |
| `app/restart_consistency.py` | **Novo**: `revoke_stale_real_consent_after_restart(session)` faz UPDATE condicional e transacional apenas em `RealRobotConsent.revoked_at IS NULL` | Revogação idempotente, sem gerar ordens, apagar autorizações históricas ou tocar nos registros de contratos |
| `app/real_runtime.py` | `reset_volatile_runtime_on_start()` limpa `running`, diagnósticos de sessão e locks locais antes de criar tarefas novas | Nenhum worker REAL reiniciado automaticamente |
| `app/main.py` | `prepare_service_restart()` normaliza estado local, limpa preços antigos, chama revogação de consentimentos REAL e preserva a rotina existente de retomada DEMO condicionada | Memória pós-boot coerente com a persistência e ausência de retomada REAL implícita |
| `app/main.py` | A retomada DEMO também recusa perfil salvo com `account_mode='real'` | Consentimento DEMO antigo não pode armar modalidade diferente |
| `app/main.py` | `market_selfcheck_task` incluída na declaração global do ciclo de vida | Todas as tarefas iniciadas são consideradas no cancelamento de encerramento |
| `tests/test_p004_restart_consistency.py` | **Novo**: cenários SQLite com sessões novas, consentimento, modo, histórico, falha e tarefa | Regressão das funcionalidades anteriores sem acesso à Deriv |
| `tests/test_postgres_p004_restart.py` | **Novo**: teste persistente e duas inicializações concorrentes em processos independentes | Idempotência e conservação dos registros no PostgreSQL descartável |
| `.github/workflows/test.yml` | CI restrito à branch P0-04 e ampliação da suíte efêmera | Nenhuma variável financeira é liberada; serviço temporário é isolado |

Nenhuma alteração foi feita em `app/db.py` (schema existente), `app/real_broker.py` (envio financeiro), autenticação, planos ou pagamentos.

## Comportamentos verificados

| Cenário | Critério de aceite |
| --- | --- |
| Processo REAL em memória + consentimento prévio ainda válido | Após cold boot `running` fica vazio; consentimento persistido ganha `revoked_at`; exige nova ação explícita autorizada, sem pedido financeiro |
| Boot repetido ou dois processos simultâneos | UPDATE condicional; apenas registros não revogados são alterados; repetição não gera mudanças |
| REAL PENDING, UNKNOWN e CONFIRMED existentes | Estado, `active_slot`, chave idempotente, `contract_id` e P/L não são modificados na preparação de boot |
| DEMO sem consentimento de retomada | Permanece desligado, mesmo com `RobotRun` persistido |
| DEMO com consentimento explícito, perfil DEMO e sessão sintética válida | Rotina legada de retomada continua disponível, sem chamar broker nos testes |
| DEMO com perfil REAL, sessão expirada ou ordem incerta | Retomada bloqueada; contrato e histórico não são modificados |
| Falha do banco durante revogação | Inicialização é interrompida antes de restaurar DEMO ou iniciar workers; nenhum REAL é armado |
| Preços, progresso de varredura e temporizadores em memória antes do boot | Dados temporários removidos, evitando apresentar caches antigos como atuais |
| Encerramento da aplicação | `market_selfcheck_task` e demais tarefas iniciadas são canceladas |
| Consulta de status após reinício com `RobotRun` antigo | Status informa `running=False` e conserva `interrupted_after_restart=True`; não simula atividade |

## Fronteiras e limitações expressas

1. A P0-04 não liquida nem cancela contratos REAL. A reconciliação de contratos com identificador e a quarentena conservadora de PENDING continuam responsabilidade dos fluxos existentes da P0-02. Nenhum resultado pode ser presumido pelo horário.
2. A retomada DEMO mantém a semântica anterior de consentimento explícito e verificações. A mudança de inicialização é **fail-closed**, não um novo mecanismo de ativação.
3. Cada processo do serviço aplica a política de não retomada REAL; inicialização simultânea de múltiplos workers pode revogar consentimento residual de um processo anterior. Esse comportamento é deliberadamente conservador e deve ser avaliado na homologação da topologia real do servidor.
4. O diagnóstico visual/status integral da interface pertence à P0-05. A P0-04 não modifica a rota `/api/status`, apenas preserva suas evidências de interrupção.
5. Os testes descartáveis não são evidência de conformidade para produção nem autorizam envio de ordens financeiras.

## CI e validações

- Suíte geral executada com APIs financeiras mockadas e `NEXORA_DEMO_BUY_ENABLED=false`.
- Serviço PostgreSQL 16 localhost `127.0.0.1:5432/nexora_p0_ci`, usuário fictício `p0_ci`. Os testes rejeitam DSNs que não correspondam ao ambiente descartável.
- O CI utiliza `NEXORA_REAL_AUTOPILOT_ENABLED=false` e `NEXORA_REAL_RELEASE_APPROVED=false`; a P0-04 não altera estas variáveis no código ou no Render.
- O teste de carga somente leitura é mantido.
- **CI da implementação:** commit `bfa846cee9b08a274d150ae9970e29a5e248a345`, execução [GitHub Actions #44624990655](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44624990655), **aprovada**. Suíte geral: **406 aprovados, 12 omitidos e 1 aviso**; carga somente leitura: **1 aprovado**; PostgreSQL 16 descartável: **12 aprovados**; job visual Chromium da P0-01 omitido (a interface não foi modificada).
- **Correção exclusivamente de fixture:** o primeiro CI PostgreSQL encontrou quatro consentimentos REAL não revogados preexistentes no banco efêmero compartilhado; um teste esperava somente um. A rotina estava correta e revogou todos. O teste passou a contar todos os registros pendentes antes do cold start, sem modificar a lógica do sistema.
- Este relatório gera um commit documental posterior ao SHA de implementação. O resultado do CI desse SHA posterior deve ser verificado separadamente.

## Pendências anteriores preservadas

- Conferir no Render autorizado as duas flags administrativas REAL efetivas antes de qualquer implantação.
- Identificar consumidores externos das APIs legadas de configuração e avaliar migração para o endpoint atômico P0-01.
- PRs #45, #46 e #47 permanecem sob revisão, não mescladas.

**PR de revisão:** [#48](https://github.com/nexoriatrader/Nexoria-Ia/pull/48), aberta em rascunho, baseada na branch da P0-03; `main` não alterada. As PRs #45–#47 permanecem abertas e não mescladas.

**Conclusão de escopo:** proteger o ciclo de vida e a persistência durante reinicializações sem avançar para P0-05, sem tocar no Supabase de produção e sem efetuar operações financeiras.
