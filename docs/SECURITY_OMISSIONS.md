# Itens deliberadamente omitidos

O backup não contém arquivos `.env` reais, senha ou connection string do Supabase, chave Fernet real, tokens OAuth, cookies, banco SQLite local, dump do Supabase ou logs com dados de sessão.

O arquivo `source/PROJECT_ENDPOINTS_PUBLIC.json` contém apenas referências públicas e marca o link de afiliado como omitido. O arquivo original com informações operacionais não foi copiado.

Para voltar a operar, os segredos devem ser recuperados do painel Render/Supabase/Deriv ou do gerenciador de segredos responsável. Nunca coloque esses valores no GitHub ou em um novo ZIP público.

## Minimização de informações públicas

O manifesto redundante `PROJECT_ENDPOINTS.json` foi retirado desta branch. `PROJECT_ENDPOINTS_PUBLIC.json` contém somente referências públicas de navegação. Identificadores internos, links administrativos e campos relacionados a integrações não devem ser duplicados nesses manifestos. Nenhuma credencial ou configuração de produção foi alterada. A remoção de arquivos não elimina informações de commits anteriores; credenciais eventualmente expostas devem ser avaliadas pelo responsável antes de qualquer ação de revogação ou rotação.
