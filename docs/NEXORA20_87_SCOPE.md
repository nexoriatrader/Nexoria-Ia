# NEXORA Intelligence 2.0 — rastreabilidade dos 87 itens

Avaliação conservadora baseada no código da main e na PR integrada #75. **Nomes de
módulos, telas, fixtures sintéticos e testes não equivalem a entrega completa.**
Mudanças da PR só estarão disponíveis no site depois do CI e da publicação.

| Itens | Bloco | Estado atual e lacuna |
|---|---|---|
| 1–3 | Modos | PARCIAL: três abas de pesquisa/anotação; experiência avançada ainda incompleta |
| 4–8 | Market | PARCIAL: OHLC público via I01, importação histórica M1 e comparação descritiva entre duas pesquisas próprias na PR; monitoramento de mercados e comparação ampla ainda faltam |
| 9–12 | Asset | PARCIAL: scanner I02 e classificações, nem tudo integrado ao painel |
| 13–18 | Context | MÓDULO EXISTENTE: I03; aplicação ao OHLC de upload na PR |
| 19–22 | Timeframe | MÓDULO EXISTENTE: I03; agregação M1/M5/M15 e alinhamento na PR |
| 23–26 | Indicators | PARCIAL: EMA/RSI e contexto; avaliação ampla de indicadores falta |
| 27–31 | Knowledge Base | PARCIAL: histórico individual de visitas e pesquisas OHLC com evidências no PostgreSQL na PR #75; atualização validada de conhecimento ainda falta |
| 32–36 | Strategy | PARCIAL: strategy_lab.py; comparação contextual global falta |
| 37–42 | Statistics | PARCIAL: distribuições, frequência, volatilidade, amplitudes e comparação cronológica entre metades no novo relatório; séries multiativos ainda faltam |
| 43–48 | Learning | NÃO CONCLUÍDO: sem modelo ML validado nem champion/challenger/fallback executável |
| 49–57 | Signals | PARCIAL: anotações locais e histórico pessoal de visitas de pesquisa; gestão completa de sinais e fornecedores ainda falta |
| 58–63 | Advanced | PARCIAL: upload OHLC e símbolo; configurações de pesquisa completas faltam |
| 64–69 | Evidence | PARCIAL: SHA-256 de entrada/saída, versões, relatórios e replay; exportação JSON integral por conta e verificador de checksums offline sem banco ou corretora na PR #75; estudos consolidados multiativos ainda faltam |
| 70–75 | Orchestrator | PARCIAL: execução sequencial determinística, etapas auditáveis; fila e distribuição robusta faltam |
| 76–87 | Dashboard | PARCIAL: painel modernizado, textos DEMO/pesquisa esclarecidos e interface de importação OHLC, histórico pessoal, comparação e relatórios na PR #75; visão geral de todos os módulos ainda falta |

## Limites comprováveis do novo fluxo

- Recebe dados históricos **enviados pelo usuário** e rotulados
  USER_SUPPLIED_UNVERIFIED_OHLC; não certifica origem de corretora.
- Salva entrada normalizada, saída, hash, versão e evidência em
  historical_research_reports, **tabela já existente**, isolando registros
  pela identidade da sessão autenticada; até cinco pesquisas por conta.
- Reproduz a análise sem criar operações, alterações de conta ou novas
  tabelas. Não cria/recomenda ordens nem ativa a conta REAL.
- O robô DEMO existente, que utiliza operações simuladas com saldo virtual, é um subsistema separado e não é alterado nesta PR.
- Sem modelo treinado comprovado. Champion/challenger/fallback não pode ser
  marcado como implementado apenas pela existência de campos nulos.
- A PR #75 consolida as antigas PRs #72 (histórico), #73 (visual) e #74 (OHLC).
  Essas PRs permanecem rastreáveis, mas a PR integrada continua sem merge.
- A aprovação exige CI completo, testes de isolamento e revisão das
  evidências; falhas de infraestrutura no CI não contam como aprovação.
