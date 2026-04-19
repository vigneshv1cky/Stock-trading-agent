# My AWS Deployment Journey: Stock Screener

This document chronicles the steps I took to transform my local Python stock screener into a live, scalable web application, and the hurdles I overcame along the way.

---

## 1. Refactoring the Application
I started with a command-line Python tool named `etf-sentiment-analyzer` that evaluated stocks based on market data and FinBERT AI sentiment analysis.
*   **The First Hurdle:** The codebase was incorrectly named and filtered out stocks over $100 (a relic from before fractional shares were common).
*   **The Fix:** I performed a complete refactor of the codebase, renaming it to `stock-sentiment`, removing the artificial `$100` price limit, and updating all the SQLite history schemas and CLI arguments to analyze the full stock universe.

## 2. Converting from CLI to Web App
I realized that reading dense financial data in a terminal was a bad user experience.
*   **The Upgrade:** I integrated `FastAPI` and created a new entry point (`web.py`). I built a modern, dark-mode vanilla HTML/CSS/JS frontend that allows users to interactively set parameters ("Min Return" and "Top N") and click a button to run the AI analysis.
*   **UI Enhancements:** I further improved the UI by adding auto-load capabilities, keeping the control panel visible after the report generates, and creating a custom Python function to draw inline SVG "sparkline" charts showing the 3-month trend for every stock.

## 3. Containerizing with Docker
To put this code on AWS, it had to be packaged so it would run identically in the cloud.
*   **The Setup:** I wrote a `Dockerfile` that installed Python, my dependencies, and pulled the FinBERT AI model directly into the image so it wouldn't have to download it on every boot. I set the container to expose Port `8080`.
*   **The Hurdle:** When I first tried to run my AWS deployment script, it failed with a `Cannot connect to the Docker daemon` error.
*   **The Fix:** I learned that Docker Desktop is the actual engine ("daemon") that builds the container. Opening the Docker Desktop app on my Mac resolved the issue, allowing the script to package my website.

## 4. Pushing to AWS ECR
I wrote an automated bash script (`deploy_ecs_express.sh`) to handle uploading the container.
*   **The Hurdle:** The script initially failed with `Unable to locate credentials` and `Token has expired` errors.
*   **The Fix:** I navigated the AWS Single Sign-On (SSO) process. I learned the difference between a local "SSO Session Name" and my actual AWS IAM Identity (`UserOne`). By running `aws configure sso` and exporting my `AWS_PROFILE`, I successfully authenticated my terminal and securely pushed my Docker image to a brand new Amazon ECR repository.

## 5. Deploying to Amazon ECS Express Mode (Fargate)
With the code in AWS, I used the ECS Console to launch the website.
*   **The Hurdle:** After my first deployment attempt, the container kept crashing in an endless loop (`tasks failed to start`).
*   **The Fix (Part 1 - Ports):** I realized AWS defaulted to looking for my app on port `8000`, but my Dockerfile was running it on port `8080`. I corrected the configuration in the ECS console.
*   **The Fix (Part 2 - Health Checks):** AWS's Load Balancer requires an instant "I'm healthy" response to know the server is alive. Since my screener took minutes to run, it failed the check. I went back into `web.py` and added a lightning-fast `GET /health` endpoint, rebuilt the Docker image, and updated the ECS service. The container successfully booted!

## 6. The 60-Second Timeout Limit
Once the website was live on the internet, I encountered one final problem.
*   **The Hurdle:** When I clicked "Run Screener" on the live site, it would spin for exactly 60 seconds and then crash with a "Server Error" or "504 Gateway Timeout".
*   **The Fix:** I discovered that the AWS Application Load Balancer has a strict "Idle Timeout" of 60 seconds. Because my FinBERT analysis takes 2-3 minutes, the Load Balancer was killing the connection mid-flight. I used the AWS CLI to dive into the Load Balancer attributes and increase the timeout to `300` seconds (5 minutes). The screener successfully generated the final report!

## 7. Domain Registration (Route 53)
I wanted a custom URL (`stockbotusa.com`) instead of the default Amazon link.
*   **The Hurdle:** My attempt to buy the domain in AWS Route 53 failed instantly.
*   **The Fix:** I learned that AWS places automated fraud-prevention holds on domain purchases for new accounts. I resolved this by opening a standard AWS Support Ticket to verify my identity and lift the restriction.

---

**Final Result:** I successfully transformed a local, terminal-based Python script into a fully containerized, serverless web application running on AWS Fargate, capable of performing complex AI sentiment analysis on demand!
