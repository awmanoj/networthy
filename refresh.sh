sed -i 's/^version = "0.1.0"/version = "0.1.2"/' pyproject.toml
git commit -am "Bump to 0.1.1" && git push
gh release create v0.1.1 --title "v0.1.1" --notes "Livelier empty dashboard"
source .env && git pull origin main && bash deploy.sh && bash run.sh
