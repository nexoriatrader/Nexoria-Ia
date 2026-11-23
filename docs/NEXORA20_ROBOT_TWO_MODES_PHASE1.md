# NEXORA 2.0 — configurador Inteligente / Manual (entrega incremental)

**Escopo:** PR #66 em DRAFT, sem merge, deploy ou migração de produção. Esta entrega não autoriza REAL e não cria novas ordens no fluxo manual.

## Experiência no painel

Ao clicar **Configurar robô**, o usuário vê somente duas opções:
- **NEXORA Inteligente:** entrada → ciclos (sim/não, quantidade, fator e exemplo) → Stop Win / Stop Loss → revisão.
- **NEXORA Manual:** entrada → nenhuma recuperação / ciclos no próximo sinal / Martingale na próxima vela → Stop Win / Stop Loss → revisão.

Ambos seguem cinco telas (seleção de modo, entrada, recuperação, limites, revisão). Nunca permitir Martingale e ciclos simultâneos. O exemplo financeiro é apenas aritmético e não promete ganhos. Valores são salvos no backend DEMO existente. Salvar não liga o robô.

O painel mostra **Agendar sinais** somente quando o modo manual está salvo no servidor. A agenda suporta horário local (gravado em UTC), ativo do catálogo público confirmado, CALL/PUT, expiração 1/3/5/15 minutos, listagem e exclusão por conta autenticada. Limites: 50 sinais futuros, entre 1 minuto e 30 dias. Nenhum sinal é enviado à corretora.

## Persistência e separação

- `robot_usage_modes` grava o modo por usuário; ausência de registro retorna `intelligent` para preservar o robô existente.
- `robot_manual_signals` grava apenas sinais de planejamento por usuário, nunca registros de ordens.
- GET/POST `/api/robot/mode`, GET/POST `/api/robot/manual-signals`, DELETE `/api/robot/manual-signals/{signal_id}`.
- A rota `/api/autopilot/start` recusa modo Manual: jamais deve iniciar silenciosamente o scanner automático nesse modo.
- O botão **LIGAR ROBÔ** fica indisponível em Manual, com texto **MANUAL · SOMENTE AGENDA**. O controle de desligar e os bloqueios no servidor continuam preservados.
- Modos são escolhas de interface e execução; `intelligent` aproveita o scanner DEMO atual (PULSO V2), **não** representa o motor completo NEXORA Intelligence 2.0. Este último permanece um serviço de pesquisa em homologação.

## Migração formal

Arquivo: `migrations/20261123_robot_modes_manual_signals_homologation.sql`.

**Não executar em produção.** A migração deve ser revisada e aplicada apenas ao PostgreSQL de homologação aprovado, antes de iniciar a nova versão ali. A inicialização PostgreSQL executa somente verificação de schema (zero DDL automático). Preparar rollback e backup de homologação antes de qualquer aplicação. A ausência das tabelas impede o serviço de iniciar: falhar fechado é intencional.

## Testes e limites da entrega

`tests/test_nexora20_robot_modes_ui.py` protege o DOM, os dois caminhos, a agenda isolada por sessão, ausência de compras no módulo de agendamento, bloqueio de start manual e necessidade de migração formal. Os testes já existentes do robô e painel seguem no workflow.

**Ainda não implementado:** encaminhamento dos sinais agendados ao executor DEMO, reconciliação de horários e contratos, tratamento de velas/expiração na execução programada, vinculação do scanner do robô ao motor Intelligence 2.0. Essa integração requer trabalho posterior e testes E2E de homologação, sem pular controles de risco nem habilitar REAL.

## Orientações operacionais

1. Revisar PR e CI na SHA final.
2. Executar migração formal somente no banco de homologação aprovado.
3. Testar com duas contas (A/B), horários em fusos distintos, duplicações, troca de modo com robô ligado, falhas de rede, reinício e exclusão de sinais.
4. Sem aprovação explícita e CI verde, não fazer merge nem deploy.


## Complemento — desempenho, rascunho atômico e consulta Intelligence (23/11/2026)

Esta etapa foi aprimorada sem alterar execução financeira:

- **Abertura da configuração:** `go('robot-config')` exibe imediatamente o assistente de cinco etapas e chama apenas `GET /api/robot/wizard`, consulta ao PostgreSQL local da aplicação. Não inicia o assistente antigo, não consulta o catálogo Deriv e não aguarda APIs externas. O carregamento utiliza um identificador de requisição para ignorar respostas obsoletas e não sobrescrever edições do usuário.
- **Gravação atômica e honesta:** `POST /api/robot/wizard` valida modo, texto decimal, limites e exclusão mútua entre ciclos/Martingale; grava modo + `preferences_json` em uma única transação. O campo é **rascunho educacional**, não aplicado ao executor DEMO/REAL, não concede consentimento e não inicia nenhuma operação. Não há gravação parcial entre três endpoints legados.
- **Agenda manual segura:** os registros continuam isolados por usuário; horário vencido aparece como `HORARIO_PASSOU_SEM_EXECUCAO` (não é um resultado de contrato). A tabela impõe unicidade de usuário, ativo, direção e horário; o servidor usa bloqueio do registro do usuário ao validar limite e duplicatas, verifica novamente a validade do horário após consulta ao catálogo e devolve conflito quando concorrentes tentam criar o mesmo item. Uma atualização manual da lista não realiza compras.
- **Intelligence 2.0:** um botão opcional, visível apenas quando o modo Inteligente está selecionado, consulta a visão pública `/api/homologation/robot/research?view=autonomous`. Exige `read_only=true`, `execution_allowed=false` e `orders_sent=0` antes de exibir informações. O retorno reflete **somente disponibilidade/integridade da pesquisa**, nunca recomendações de CALL/PUT, previsões ou operações; a indisponibilidade é mostrada explicitamente.
- **Banco e implantação:** migração formal inclui `robot_usage_modes.preferences_json`, integridade de agenda e índices. O preflight de PostgreSQL falha quando a coluna obrigatória está ausente; não gera DDL no startup. Não houve nenhuma migração de produção, merge nem deploy.
- **Testes novos:** `tests/test_nexora20_wizard_draft_e2e.py` cobre requisições autenticadas com SQLite descartável, gravação atômica, ausência de compras, isolamento A/B, tentativas inválidas, duplicidade DB/API, dados após nova sessão, horários expirados, bloqueio de start manual e consulta sem dependência do catálogo. Todos os testes devem ser aprovados no SHA final antes de qualquer homologação.

### O que permanece pendente

O **robô Inteligente existente** segue utilizando o motor de execução DEMO anterior: o motor completo Intelligence 2.0 é apenas pesquisa de qualidade e não pode ser apresentado como decisão de ordens. O **modo Manual** guarda agendamentos e nunca executa ordens automaticamente. Os valores desse novo assistente são rascunhos e não parâmetros ativos do executor. Não foram implementados compra de contratos, martingale em uma mesma vela, rearmamento automático, ativação REAL, migrations produtivas ou deploy. Homologação operacional e testes end-to-end de navegador/infraestrutura real precisam ser executados antes de declarar o produto em produção.

