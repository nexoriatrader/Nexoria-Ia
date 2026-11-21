# NEXORA Intelligence 2.0 — rastreabilidade dos 87 itens

Avaliação conservadora baseada no código da main e na PR #74. **Nomes de
módulos, telas, fixtures sintéticos e testes não equivalem a entrega completa.**
Mudanças da PR só estarão disponíveis no site depois do CI e da publicação.

| Itens | Bloco | Estado atual e lacuna |
|---|---|---|
| 1–3 | Modos | PARCIAL: três abas de pesquisa/anotação; experiência avançada ainda incompleta |
| 4–8 | Market | PARCIAL: OHLC público via I01 e upload histórico M1 na PR; monitoramento comparativo amplo falta |
| 9–12 | Asset | PARCIAL: scanner I02 e classificações, nem tudo integrado ao painel |
| 13–18 | Context | MÓDULO EXISTENTE: I03; aplicação ao OHLC de upload na PR |
| 19–22 | Timeframe | MÓDULO EXISTENTE: I03; agregação M1/M5/M15 e alinhamento na PR |
| 23–26 | Indicators | PARCIAL: EMA/RSI e contexto; avaliação ampla de indicadores falta |
| 27–31 | Knowledge Base | PARCIAL: histórico/evidência PostgreSQL na PR; aprendizado validado e atualização faltam |
| 32–36 | Strategy | PARCIAL: strategy_lab.py; comparação contextual global falta |
| 37–42 | Statistics | PARCIAL: distribuições, frequência, volatilidade e amplitudes no novo relatório; comparações extensas faltam |
| 43–48 | Learning | NÃO CONCLUÍDO: sem modelo ML validado nem champion/challenger/fallback executável |
| 49–57 | Signals | PARCIAL: anotações locais; importação/fornecedores/validadores/histórico definitivo faltam |
| 58–63 | Advanced | PARCIAL: upload OHLC e símbolo; configurações de pesquisa completas faltam |
| 64–69 | Evidence | PARCIAL: evidências SHA-256, versão, input/resultado persistidos e replay na PR; pipeline de relatórios abrangente falta |
| 70–75 | Orchestrator | PARCIAL: execução sequencial determinística, etapas auditáveis; fila e distribuição robusta faltam |
| 76–87 | Dashboard | PARCIAL: painel e pesquisa por arquivo, histórico e leitura de relatórios na PR; visão consolidada ainda falta |

## Limites comprováveis do novo fluxo

- Recebe dados históricos **enviados pelo usuário** e rotulados
  USER_SUPPLIED_UNVERIFIED_OHLC; não certifica origem de corretora.
- Salva entrada normalizada, saída, hash, versão e evidência em
  historical_research_reports, **tabela já existente**, isolando registros
  pela identidade da sessão autenticada; até cinco pesquisas por conta.
- Reproduz a análise sem criar operações, alterações de conta ou novas
  tabelas. Não cria/recomenda ordens nem ativa a conta REAL.
- O robô DEMO existente é um subsistema separado e não é alterado nesta PR.
- Sem modelo treinado comprovado. Champion/challenger/fallback não pode ser
  marcado como implementado apenas pela existência de campos nulos.
- A aprovação exige CI completo, testes de isolamento e revisão das
  evidências; falhas de infraestrutura no CI não contam como aprovação.
