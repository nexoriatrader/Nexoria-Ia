# Monitor temporário de atividade do NEXORA no Render Free

**Objetivo:** enviar uma consulta HTTP pública a `https://nexoria-ia.onrender.com/healthz` a cada 10 minutos (GitHub Actions), reduzindo o risco de o serviço Render Free hibernar após 15 minutos sem tráfego.

- Workflow: `.github/workflows/nexora-keepawake.yml`
- Gatilhos: agendamento e disparo manual (workflow_dispatch)
- Rota: `GET /healthz` — não precisa de login, não envia ordens e não configura o AutoPilot
- Runner: `ubuntu-slim` (opção mais econômica no GitHub Actions)
- Segurança: não usa senhas, OAuth, tokens da Deriv nem chamadas de compra
- O GitHub pode atrasar ou pular agendamentos: **não é garantia de 24/7**.
- Render Free pode reiniciar por outros motivos e tem limite de 750 horas de instância Free por mês, compartilhado com outros serviços do workspace.
- O AutoPilot NÃO inicia sozinho após reinícios: é preciso conferir a situação no painel.
- Um workflow de 10 minutos pode executar cerca de 4.320 vezes por mês, consumindo cota de GitHub Actions. O `ubuntu-slim` custa menos por minuto, mas ainda pode consumir toda a franquia ou gerar cobranças conforme o plano e outros workflows. Consulte o painel de Billing do GitHub.
- Substitua por hospedagem paga e remova/disable o monitor após a migração.

Para interromper: GitHub > Actions > NEXORA IA — Monitor temporário do Render > menu > Disable workflow. Se preferir, remova o arquivo do repositório.
