# Segurança GitHub — NEXORA IA

Atualização: 25/11/2026. A auditoria histórica de 17/11/2026 descrevia um repositório privado. A consulta de 25/11/2026 confirma que o repositório está público e a branch main ainda não está protegida.

## Segurança no GitHub Free

A opção de manter o repositório público não torna públicos os dados dos usuários nem autoriza a publicação de credenciais. Repositórios públicos no GitHub Free podem usar proteção de branch. Referência: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

- Proteger main nas configurações do repositório: exigir pull request e verificações de CI bem-sucedidas, impedir force-push e exclusão da branch. Evitar bypass das regras.
- Ativar autenticação de dois fatores ou passkey e revisar permissões de aplicativos conectados e chaves de acesso.
- Habilitar os recursos disponíveis de secret scanning e alertas de dependências.
- Manter credenciais, dados de usuários, arquivos de backup e chaves de criptografia fora do Git; usar armazenamento privado e permissões mínimas.
- Revisar todo o histórico Git, branches, PRs, issues e artefatos. Se uma credencial tiver sido exposta, revogá-la e rotacioná-la; excluir um arquivo não apaga seu histórico.
- Não publicar logs com dados pessoais nem copiar valores de variáveis privadas para comentários ou documentos.

## Auditoria automatizada

O workflow de segurança e o script de auditoria do repositório examinam padrões de segredos nos arquivos versionados de HEAD. Uma aprovação não comprova ausência de exposição no histórico, em artefatos ou no ambiente de execução.

## Publicação controlada

A branch main está associada ao deploy automático. Usar branches isoladas, revisão e verificações antes de publicar mudanças. Manter os controles de autorização e os bloqueios de segurança existentes; esta revisão documental não autoriza a alteração de funcionalidades financeiras, configuração de produção ou permissões de usuários.

A proteção de branch, a revisão histórica e a segurança efetiva do ambiente ainda precisam de confirmação independente.
