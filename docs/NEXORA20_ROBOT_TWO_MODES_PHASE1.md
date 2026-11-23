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
