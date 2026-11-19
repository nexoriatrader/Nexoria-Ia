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
3. Cada processo aplica a política de não retomada REAL. A P0-04 passou a incluir sincronização periódica de consentimentos e bloqueio transacional PostgreSQL no último ponto antes do envio. Uma revogação **confirmada antes da checagem** impede o envio; uma revogação concorrente precisa esperar a liberação do bloqueio. Uma requisição já iniciada **antes da revogação ser confirmada** não pode ser desfeita por esse mecanismo. A topologia multiworker e a duração da transação aberta durante a chamada externa exigem revisão de operação antes de implantar.
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


## Revisão independente da P0-04 — concorrência multiworker e último consentimento

**Problema confirmado:** `prepare_service_restart()` revogava `RealRobotConsent` no PostgreSQL, mas o `running` de processos já ativos permanecia em memória. O scanner também verificava consentimento antes de criar uma reserva, porém, após a reserva, só repetia verificações locais de `running` e deadline. Outro processo poderia revogar o consentimento no intervalo.

### Correções adicionais, sem liberar execução REAL

1. **`app/restart_consistency.py::locked_real_consent_for_send`:** consulta o consentimento persistido em uma sessão nova e exige identidade de usuário, conta idêntica e `revoked_at IS NULL`, usando `SELECT ... FOR UPDATE` no PostgreSQL. A transação permanece aberta **até a chamada existente terminar**, serializando a leitura final com qualquer `UPDATE` de revogação. Se a revogação já foi confirmada, o retorno é falso. Em mecanismo sem bloqueio por linha (por exemplo, SQLite), retorna falso por segurança. O guard não cria, reativa ou altera consentimentos ou contratos.
2. **`app/real_runtime.py::_user_scan`:** realiza essa última checagem **depois da reserva e imediatamente antes do adaptador financeiro já existente**. Também reavalia estado local, flags administrativas e expiração dentro da janela protegida. Se o consentimento estiver revogado, desarma localmente o worker **sem enviar** e mantém a reserva `PENDING` intacta, conservadoramente bloqueada para revisão.
3. **`app/real_runtime.py::disarm_revoked_real_workers`:** o loop observa periodicamente o banco compartilhado, descarta `running` locais sem consentimento persistido válido e registra o motivo. O conjunto local não é mais considerado autorização suficiente. Isso não altera registros no banco e **não substitui** o bloqueio transacional imediatamente antes do envio.
4. **Falha da validação:** erro da sessão de autorização antes de qualquer chamada externa causa interrupção conservadora e preserva a reserva. Se uma chamada já foi iniciada e seu resultado se torna incerto, o tratamento anterior de `UNKNOWN` é mantido.
5. **Testes legados:** dois testes em SQLite que simulavam o envio usando `AsyncMock` receberam um substituto **estritamente de teste** para o guard, a fim de preservar a regressão de contrato fictício. O código de produção permanece fail-closed fora do PostgreSQL. Nenhuma flag administrativa do ambiente de execução foi ativada.

### Evidências entre processos em PostgreSQL descartável

- `test_revoked_between_reservation_and_send_blocks_scanner_in_another_process`: um processo executa o scanner verdadeiro com sinal e adaptador de compra substituídos por objetos sintéticos e pausa depois de persistir uma única reserva `PENDING`. Um segundo processo confirma a revogação pelo banco compartilhado. Ao prosseguir, o scanner verifica o consentimento revogado, **não chama o adaptador**, desarma o worker, preserva exatamente a reserva `PENDING` e não modifica os registros preexistentes `UNKNOWN` e `CONFIRMED`.
- `test_postgresql_consent_lock_serializes_outbound_boundary_and_revocation`: um processo mantém o bloqueio de linha enquanto grava apenas um marcador de envio **fictício**, sem rede. O outro tenta revogar o consentimento em transação independente. Comprova-se que a revogação não confirma durante o bloqueio e que o marcador anterior precede a confirmação da revogação. Não há envio Deriv em nenhum teste.
- Testes locais cobrem o desarme ao encontrar consentimento revogado ou ausente, a preservação do consentimento de outro usuário e o bloqueio em SQLite.

**Garantia demonstrada:** a coordenação PostgreSQL impede uma *nova chamada* após revogação persistida/confirmada, desde que o ponto de envio e os processos revogadores utilizem o mesmo banco e o guard instalado. Uma solicitação transmitida antes da confirmação não pode ser retirada: esse risco prévio permanece sob reconciliação conservadora. Não há garantia de cancelamento retroativo ou de que a corretora não recebeu uma solicitação anterior.

**Limitação operacional a revisar:** a transação do bloqueio PostgreSQL permanece aberta enquanto a chamada de envio ocorre; isso pode atrasar revogações concorrentes e deve ser avaliado na revisão da topologia Render antes de qualquer implantação. Em falha de conectividade com o banco antes da autorização, o caminho bloqueia novos envios.

### Reexecução de regressão

**SHA de código e testes:** `94d80edc2fa8380ed6b4fbd09664af2a597439b9` — [CI #44627464903](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44627464903), aprovado: **410 testes gerais aprovados, 14 omitidos, 1 aviso; 1 teste de carga somente leitura aprovado; 14 testes PostgreSQL descartáveis aprovados**. O job Chromium foi omitido, pois a interface não foi alterada.

**Atenção à proveniência:** o commit que inclui esta atualização documental tem SHA próprio. Confirmar o respectivo CI separadamente antes da aprovação da PR #48. Nenhuma implantação, merge, migração de produção, alteração de flags ou operação financeira foi realizada. A P0-05 não foi iniciada.
