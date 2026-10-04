FROM public.ecr.aws/lambda/python:3.12

WORKDIR /var/task

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Lambda invokes app.handler; uvicorn is used for local/App Runner
CMD ["app.handler"]
